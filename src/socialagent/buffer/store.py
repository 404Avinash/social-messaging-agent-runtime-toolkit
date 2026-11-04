# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persistence for the message-debounce buffer (control-layer / Lambda side).

Self-contained DynamoDB access (does NOT extend :class:`socialagent.state.table.SocialAgentState`)
so the buffering layer can use primitives that SocialAgentState doesn't expose:
append-only writes and an atomic counter for the generation token.

Schema — reuses the shared state table (``pk``/``sk``/``data``/``ttl``) with a
dedicated key namespace so it never collides with agent state:

    pk = "buf#{phone}"
    sk = "msg#{seq:020d}#{message_id}"   ← one item per inbound message (append-only)
    sk = "meta"                          ← per-phone version stamp / counters

Why append-only: two messages arriving ~1s apart must never clobber each other.
Each message is its own ``put_item`` keyed by a monotonic ``seq``; the flush does
a single ``query`` over the partition. The ``meta`` item holds ``latest_seq`` —
the generation token a scheduled flush compares against to detect newer messages.

Like :class:`socialagent.state.table.SocialAgentState`, the store is constructed from config
values and treats an empty / dev-mode-sentinel table name as "not configured".
Unlike SocialAgentState, an unconfigured store falls back to a temp JSON file so the
decision logic stays exercisable offline in unit tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from socialagent.constants import DEV_MODE_VALUE
from socialagent.utils.devfiles import dev_dir, private_dir, write_private

logger = logging.getLogger(__name__)

_PK_PREFIX = "buf#"
_META_SK = "meta"
_MSG_SK_PREFIX = "msg#"

# Dev-mode file store, separate from the agent state file, in the same
# owner-only per-user directory (0700, file 0600). Not concurrency-safe by
# design — buffering is disabled in dev mode, so this path is only exercised
# by unit tests that drive a single logical sequence.
_DEV_PATH = dev_dir() / "socialagent_buffer_dev.json"


def _pk(phone: str) -> str:
    return f"{_PK_PREFIX}{phone}"


def _msg_sk(seq: int, message_id: str) -> str:
    # Zero-pad seq so lexicographic sort == numeric sort in the DynamoDB query.
    return f"{_MSG_SK_PREFIX}{seq:020d}#{message_id}"


def _dev_read() -> dict:
    try:
        # Never read from a directory another user owns: it may hold planted entries.
        private_dir(_DEV_PATH.parent)
        return json.loads(_DEV_PATH.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {}
    except OSError as exc:
        # A refused dev dir (a symlink, or another user's) reads as an empty
        # buffer; say so rather than fall back silently.
        logger.warning(
            "Dev buffer %s not read (%s); treating it as empty",
            _DEV_PATH,
            type(exc).__name__,
        )
        return {}


def _dev_write(data: dict) -> None:
    write_private(_DEV_PATH, json.dumps(data))


class BufferStore:
    """DynamoDB-backed persistence for the debounce buffer.

    All methods fall back to a temp JSON file when the table name is empty or the
    dev-mode sentinel (``__socialagent_dev__``), so unit tests run fully offline.
    """

    def __init__(self, table_name: str, region: str, ttl_seconds: int) -> None:
        self._table_name = table_name
        self._region = region
        self._ttl_seconds = ttl_seconds
        self._table_obj = self._build_table()

    def _build_table(self) -> Any | None:
        """Build the DynamoDB Table once, or ``None`` when unconfigured.

        Returns ``None`` when the table name is empty or the dev-mode sentinel
        (so dev/test use the JSON-file fallback), or if the boto3 resource can't
        be created (credential/config errors degrade to the file path).
        """
        if not self._table_name or self._table_name == DEV_MODE_VALUE:
            return None
        try:
            return boto3.resource("dynamodb", region_name=self._region).Table(
                self._table_name
            )
        except Exception:  # noqa: BLE001 — fall back to dev-mode JSON on credential/config errors
            return None

    def _table(self) -> Any | None:
        """Return the cached DynamoDB Table resource, or ``None`` when unconfigured."""
        return self._table_obj

    # ── version stamp (generation token) ────────────────────────────────────
    async def next_seq(self, phone: str) -> int:
        """Atomically increment and return this phone's message sequence number.

        Uses a DynamoDB atomic counter (``ADD latest_seq 1``) so concurrent
        inbound messages each get a unique, monotonically increasing seq with no
        lost updates. The returned value is the generation token for the message
        being ingested.
        """
        table = self._table()
        if table is None:
            store = _dev_read()
            key = f"{_pk(phone)}#{_META_SK}"
            seq = int(store.get(key, {}).get("latest_seq", 0)) + 1
            store[key] = {"latest_seq": seq}
            _dev_write(store)
            return seq

        def _bump() -> int:
            resp = table.update_item(
                Key={"pk": _pk(phone), "sk": _META_SK},
                UpdateExpression="SET #t = :ttl ADD latest_seq :one",
                ExpressionAttributeNames={"#t": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":ttl": int(time.time()) + self._ttl_seconds,
                },
                ReturnValues="UPDATED_NEW",
            )
            attrs = resp.get("Attributes", {})
            if "latest_seq" not in attrs:
                raise RuntimeError("DynamoDB update_item did not return latest_seq")
            return int(attrs["latest_seq"])

        return await asyncio.to_thread(_bump)

    async def latest_seq(self, phone: str) -> int:
        """Return the current generation token (0 if no buffer exists)."""
        table = self._table()
        if table is None:
            store = _dev_read()
            return int(store.get(f"{_pk(phone)}#{_META_SK}", {}).get("latest_seq", 0))

        def _read() -> int:
            resp = table.get_item(Key={"pk": _pk(phone), "sk": _META_SK})
            item = resp.get("Item")
            return int(item["latest_seq"]) if item and "latest_seq" in item else 0

        return await asyncio.to_thread(_read)

    # ── message buffer ────────────────────────────────────────────────────────
    async def append_message(
        self,
        phone: str,
        seq: int,
        message_id: str,
        text: str,
        ts: float,
        msg_type: str = "text",
        metadata: dict | None = None,
    ) -> None:
        """Append one message to the buffer (append-only put_item).

        Keyed by ``(seq, message_id)``: SNS redelivery of the same message_id at
        the same seq overwrites itself (idempotent) rather than duplicating.

        msg_type: "text" | "image" | "audio" | "button" | "location" | "document"
        metadata: type-specific data (e.g. {"s3_key": "...", "mime": "..."} for images)
        """
        item = {
            "seq": seq,
            "message_id": message_id,
            "text": text,
            "ts": ts,
            "type": msg_type,
        }
        if metadata:
            item["metadata"] = metadata

        table = self._table()
        if table is None:
            store = _dev_read()
            store[f"{_pk(phone)}#{_msg_sk(seq, message_id)}"] = item
            _dev_write(store)
            return

        def _put() -> None:
            table.put_item(
                Item={
                    "pk": _pk(phone),
                    "sk": _msg_sk(seq, message_id),
                    "data": json.dumps(item),
                    "ttl": int(time.time()) + self._ttl_seconds,
                }
            )

        await asyncio.to_thread(_put)

    async def collect_messages(self, phone: str) -> list[dict]:
        """Return all buffered messages for a phone, ordered by seq ascending."""
        table = self._table()
        if table is None:
            store = _dev_read()
            prefix = f"{_pk(phone)}#{_MSG_SK_PREFIX}"
            msgs = [v for k, v in store.items() if k.startswith(prefix)]
            return sorted(msgs, key=lambda m: m.get("seq", 0))

        def _query() -> list[dict]:
            msgs: list[dict] = []
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": (
                    Key("pk").eq(_pk(phone)) & Key("sk").begins_with(_MSG_SK_PREFIX)
                ),
            }
            while True:
                resp = table.query(**kwargs)
                for item in resp.get("Items", []):
                    raw = item.get("data")
                    if raw:
                        msgs.append(json.loads(raw))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            return sorted(msgs, key=lambda m: m.get("seq", 0))

        return await asyncio.to_thread(_query)

    async def clear_messages(self, phone: str, messages: list[dict]) -> None:
        """Delete exactly the messages a flush consumed, nothing else.

        Prefer this over :meth:`clear` on the flush path. ``clear`` queries the
        partition at clear time and deletes everything it finds, but a flush
        snapshots the buffer and then spends 5-30s invoking the runtime — so
        anything appended in that window (a follow-up sent while the bot is
        "typing") got deleted without ever being processed, and without a retry,
        because the flush that would have handled it then saw an empty buffer.

        Keys are derived from the snapshot rather than re-read, so a concurrent
        append cannot be caught in the blast. The ``meta`` generation stamp is left
        alone: dropping it reset ``latest_seq`` to 0 and made in-flight flushes
        from the previous numbering drain the *next* burst early. Both message and
        meta items carry a TTL, so nothing accumulates.
        """
        if not messages:
            return

        sks = [
            _msg_sk(m["seq"], m["message_id"])
            for m in messages
            if m.get("message_id") is not None and m.get("seq") is not None
        ]
        if not sks:
            return

        table = self._table()
        if table is None:
            store = _dev_read()
            for sk in sks:
                store.pop(f"{_pk(phone)}#{sk}", None)
            _dev_write(store)
            return

        def _delete() -> None:
            with table.batch_writer() as batch:
                for sk in sks:
                    batch.delete_item(Key={"pk": _pk(phone), "sk": sk})

        await asyncio.to_thread(_delete)

    async def clear(self, phone: str) -> None:
        """Delete all buffered messages AND the meta/version stamp for a phone.

        Called after a successful flush. Resets the generation token to 0 so the
        next burst starts a fresh sequence.
        """
        table = self._table()
        if table is None:
            store = _dev_read()
            prefix = f"{_pk(phone)}#"
            for k in [k for k in store if k.startswith(prefix)]:
                store.pop(k, None)
            _dev_write(store)
            return

        def _purge() -> None:
            # Collect all sks under the partition (messages + meta), then batch-delete.
            sks: list[str] = []
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(_pk(phone)),
                "ProjectionExpression": "sk",
            }
            while True:
                resp = table.query(**kwargs)
                sks.extend(item["sk"] for item in resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            with table.batch_writer() as batch:
                for sk in sks:
                    batch.delete_item(Key={"pk": _pk(phone), "sk": sk})

        await asyncio.to_thread(_purge)
