# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Decision logic for the message-debounce buffer (deterministic, timer-only).

WhatsApp users split one thought across several rapid messages. Without
debouncing, each message triggers a separate agent invocation and response. The
:class:`MessageBuffer` coalesces a burst into a single invocation once the user
stops sending for ``buffer_window_seconds``, using a generation-token mechanism
that always waits from the LAST message (timer-reset semantics).

This module owns NO transport. The webhook handler and the delayed-flush consumer
call into it and perform the side effects (SQS enqueue, runtime invoke, send
reply) themselves — keeping I/O at the edges makes this unit-testable.

The buffer is constructed from an :class:`~socialagent.config.SocialAgentConfig` (matching the
``make_webhook_handler(config, ...)`` factory style), so all configuration flows
through the sanctioned ``SocialAgentConfig.from_env()`` mechanism — the buffer reads no
environment variables directly.

Two entry points:

``ingest(...)`` / ``ingest_text(...)`` — called for each inbound message.
  1. seq = atomic increment of the phone's generation token
  2. append the message to the buffer
  Returns an :class:`IngestResult` whose ``flush_now`` is always False
  (deterministic timer-only debounce): the caller enqueues a delayed flush
  carrying ``seq``.

``should_flush(phone, scheduled_seq, now)`` — called when a delayed flush fires.
  Decides whether THIS scheduled flush is still the one that should run:
    - aborts if a newer message arrived after it was scheduled (a later flush
      will handle the burst) UNLESS the max-wait cap has been exceeded
    - aborts if the buffer is already empty (a prior flush already drained it)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from socialagent.config import SocialAgentConfig
from socialagent.constants import DEV_MODE_VALUE
from socialagent.utils.phone import mask_phone

from .store import BufferStore

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Result of buffering one inbound message.

    ``flush_now`` is always False in deterministic timer-only debounce — the
    caller enqueues a delayed flush. Retained for compatibility with callers
    that branch on it.
    """

    seq: int
    flush_now: bool


@dataclass
class FlushDecision:
    """Verdict for a scheduled flush.

    ``messages`` holds the buffered messages to send (only meaningful when
    ``should_flush`` is True).
    """

    should_flush: bool
    reason: str
    messages: list[dict]


class MessageBuffer:
    """Inbound message debounce buffer, configured from an :class:`SocialAgentConfig`.

    Construct once per handler (config injected), then call :meth:`is_enabled`
    to gate the feature and the async methods to ingest / flush. Disabled in dev
    mode (no state table) regardless of the feature flag.
    """

    def __init__(self, config: SocialAgentConfig) -> None:
        self._table_name = config._state_table_name
        self._enabled_flag = config.message_buffering_enabled
        self._queue_url = config._buffer_queue_url
        self._window_seconds = config.buffer_window_seconds
        self._max_wait_seconds = config.buffer_max_wait_seconds
        self._store = BufferStore(
            table_name=config._state_table_name,
            region=config.region,
            ttl_seconds=config.buffer_ttl_seconds,
        )

    @property
    def window_seconds(self) -> int:
        """Debounce window (also the SQS DelaySeconds the caller should use)."""
        return self._window_seconds

    def is_enabled(self) -> bool:
        """True only when buffering is on and everything the flush path needs exists.

        Dev mode (no state table, or the ``__socialagent_dev__`` sentinel) always
        disables buffering — per-message behavior is what you want in the
        single-user ``socialagent dev`` REPL.

        The flush queue is checked here too, not just where it is used. Without it
        ``send_flush`` silently no-ops, so a buffered message was accepted, logged
        as "flush scheduled", and then never flushed or answered — every text
        swallowed with no error anywhere. Terraform keeps the two values
        consistent, but nothing in the code did. Falling back to unbuffered
        handling is the safe degradation.
        """
        if not self._table_name or self._table_name == DEV_MODE_VALUE:
            return False
        if self._enabled_flag and not self._queue_url:
            logger.warning(
                "Message buffering is enabled but BUFFER_QUEUE_URL is not set — "
                "falling back to unbuffered handling, since a buffered message "
                "could never be flushed."
            )
            return False
        return self._enabled_flag

    async def ingest(
        self,
        phone: str,
        message_id: str,
        text: str,
        now: float,
        msg_type: str = "text",
        metadata: dict | None = None,
    ) -> IngestResult:
        """Buffer one inbound message (any type).

        Deterministic debounce: every message extends the window, so this always
        returns ``flush_now=False`` — the caller enqueues a delayed flush
        carrying ``seq`` and the burst is coalesced once the window elapses (see
        :meth:`should_flush`).

        ``now`` is the caller-supplied current epoch seconds (injected for testability).
        ``msg_type``: "text" | "image" | "audio" | "button" | "location" | "document"
        ``metadata``: type-specific data (e.g. s3_key for images).
        """
        seq = await self._store.next_seq(phone)
        await self._store.append_message(
            phone, seq, message_id, text, now, msg_type=msg_type, metadata=metadata
        )
        logger.info(
            "buffer.ingest phone=%s seq=%s type=%s",
            mask_phone(phone),
            seq,
            msg_type,
        )
        return IngestResult(seq=seq, flush_now=False)

    async def ingest_text(
        self, phone: str, message_id: str, text: str, now: float
    ) -> IngestResult:
        """Backward-compatible wrapper: buffer a text message."""
        return await self.ingest(phone, message_id, text, now, msg_type="text")

    async def should_flush(
        self, phone: str, scheduled_seq: int, now: float
    ) -> FlushDecision:
        """Decide whether a delayed flush firing for ``scheduled_seq`` should run.

        The generation-token check: if ``latest_seq > scheduled_seq``, a newer
        message arrived after this flush was scheduled and scheduled its own,
        later-firing flush — so THIS one aborts to avoid an early/duplicate flush.

        Exception (max-wait cap): if the oldest buffered message is older than
        ``buffer_max_wait_seconds``, we flush anyway even though newer messages
        exist, so a user who keeps typing still gets a response.
        """
        messages = await self._store.collect_messages(phone)
        if not messages:
            return FlushDecision(False, "buffer_empty", [])

        current = await self._store.latest_seq(phone)

        if current > scheduled_seq:
            oldest_ts = min((m.get("ts", now) for m in messages), default=now)
            if now - oldest_ts >= self._max_wait_seconds:
                return FlushDecision(True, "max_wait_exceeded", messages)
            return FlushDecision(False, "superseded", [])

        return FlushDecision(True, "window_elapsed", messages)

    async def collect_messages(self, phone: str) -> list[dict]:
        """Return all buffered messages for a phone, ordered by seq ascending."""
        return await self._store.collect_messages(phone)

    async def clear(self, phone: str) -> None:
        """Delete all buffered messages and reset the generation token.

        Full purge. On the flush path prefer :meth:`clear_flushed`, which cannot
        discard a message that arrived while the runtime was being invoked.
        """
        await self._store.clear(phone)

    async def clear_flushed(self, phone: str, messages: list[dict]) -> None:
        """Retire exactly the messages a flush consumed.

        Leaves later arrivals for the next flush and preserves the generation
        stamp.
        """
        await self._store.clear_messages(phone, messages)


def combine_prompt(
    messages: list[dict],
    image_prompt: str = "[User sent an image.]",
) -> str:
    """Combine buffered messages of any type into a single agent prompt.

    Text messages are concatenated with newlines. Non-text messages are
    represented as bracketed placeholders. Override ``image_prompt`` to give the
    agent domain-specific instructions for image messages.

    Consecutive identical texts are deduplicated to prevent duplicate prompts
    from triggering Bedrock streaming issues.
    """
    out: list[str] = []
    last: str | None = None
    for m in messages:
        msg_type = m.get("type", "text")
        if msg_type == "text":
            text = m.get("text", "")
            if text and text != last:
                out.append(text)
                last = text
        elif msg_type == "image":
            out.append(image_prompt)
            last = None
        elif msg_type == "audio":
            text = m.get("text") or "[User sent a voice note]"
            out.append(text)
            last = None
        elif msg_type == "location":
            out.append(m.get("text", "[User shared location]"))
            last = None
        elif msg_type == "button":
            text = m.get("text", "")
            if text:
                out.append(text)
            last = None
        else:
            out.append(f"[User sent {msg_type}]")
            last = None
    return "\n".join(filter(None, out))
