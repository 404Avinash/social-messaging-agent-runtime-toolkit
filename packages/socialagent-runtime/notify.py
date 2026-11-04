# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Notification helpers for agents running inside AgentCore Runtime.
Reads config from socialagent_runtime.config (single source of truth for env vars).

Two modes of operation:
- Inside outbound_context(): messages are queued and returned to the webhook,
  which sends them after the agent turn completes. The webhook delivers every
  queued message to the conversation's sender and ignores log_to_memory /
  memory_text, so neither a cross-phone recipient nor a memory write is
  available on this path. The framework's session manager records the tool
  exchange (e.g. AgentCoreMemorySessionManager for Strands), but
  create_session_manager strips past toolUse/toolResult blocks when a session
  is restored, so only the agent's reply text persists across turns. To reach
  another phone from a tool, pass immediate=True, which sends now to the phone
  given (never take it from model input) and cannot be combined with
  log_to_memory.
- Outside outbound_context(): messages are sent immediately via the notification
  Lambda. Pass log_to_memory=True to write the message to AgentCore Memory.

Memory write rules (out-of-band sends only):
- send_text:     logged text = the message text itself
- send_template: logged text = rendered template body (or "[Template: name]" fallback)
- send_message:  requires memory_text when log_to_memory=True (no auto-renderable content)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from contextlib import contextmanager
from typing import Generator

import boto3

try:
    from ._devfiles import dev_dir, private_dir, write_private
    from .config import config
    from .phone import mask_phone, normalize_phone
except ImportError:
    from _devfiles import dev_dir, private_dir, write_private  # type: ignore[no-redef]
    from config import config  # type: ignore[no-redef]
    from phone import mask_phone, normalize_phone  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Per-invocation outbound queue — set by outbound_context()
_queue: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "socialagent_outbound", default=None
)

# Shared cross-process dev inbox — JSON file in a per-user, owner-only directory
# under the temp dir, so dev conversation content is not readable by other
# users on a shared host. The directory is 0700 and the file is written 0600
# (see _devfiles.py, which also refuses a directory another user created).
# Intentionally not concurrent-safe: socialagent dev is a single-user REPL.
_DEV_INBOX_DIR = dev_dir()
_DEV_INBOX_PATH = _DEV_INBOX_DIR / "socialagent_dev_inbox.json"


def _dev_inbox_write(msgs: list[dict]) -> None:
    """Write the dev inbox with owner-only (0600) permissions in a 0700 dir."""
    write_private(_DEV_INBOX_PATH, json.dumps(msgs))


def _dev_inbox_append(msg: dict) -> None:
    """Append a message to the shared dev inbox file."""
    msgs = _dev_inbox_read_all()
    msgs.append(msg)
    _dev_inbox_write(msgs)


def _dev_inbox_read_all() -> list[dict]:
    """Read all messages from the shared dev inbox file."""
    try:
        # Never read from a directory another user owns: it may hold planted messages.
        private_dir(_DEV_INBOX_PATH.parent)
        return json.loads(_DEV_INBOX_PATH.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return []
    except OSError as exc:
        # A refused dev dir (a symlink, or another user's) reads as an empty
        # inbox; say so, or the REPL just never shows the message.
        logger.warning(
            "Dev inbox %s not read (%s); treating it as empty",
            _DEV_INBOX_PATH,
            type(exc).__name__,
        )
        return []


def dev_inbox_drain_for(phone: str) -> list[dict]:
    """Return and remove messages destined for phone from the shared inbox.

    Phones are compared normalised, as the webhook compares them, so a send
    to ``+52 1 55 5555 0100`` reaches a REPL simulating ``+525555550100``.
    """
    target = normalize_phone(phone)
    mine: list[dict] = []
    remaining: list[dict] = []
    for m in _dev_inbox_read_all():
        to = normalize_phone(str(m.get("phone") or ""))
        (mine if to == target else remaining).append(m)
    _dev_inbox_write(remaining)
    return mine


@contextmanager
def outbound_context() -> Generator[list[dict], None, None]:
    """Capture outbound messages during an agent turn instead of sending immediately.

    Prefer importing this via ``socialagent_runtime.socialagent_notify_context`` for clarity.
    See :func:`socialagent_runtime.socialagent_notify_context`.
    """
    q: list[dict] = []
    token = _queue.set(q)
    try:
        yield q
    finally:
        _queue.reset(token)


def _lambda_client():
    return boto3.client("lambda", region_name=config.region)


async def send_template(
    phone: str,
    template_name: str,
    params: list[str],
    *,
    language: str = "es_MX",
    log_to_memory: bool = False,
    immediate: bool = False,
) -> None:
    """Send a WhatsApp template message.

    Inside outbound_context(): queued for webhook delivery after the agent turn,
    always to the conversation's sender. Outside: sent immediately via
    notification Lambda.

    Args:
        phone: E.164 recipient phone number.
        template_name: Template name (without prefix).
        params: Ordered body parameter values.
        language: BCP-47 language code.
        log_to_memory: Write rendered template body to AgentCore Memory after delivery.
            Only takes effect outside outbound_context(); a queued send ignores it.
        immediate: Send right now instead of queuing for end-of-turn delivery.
            See :func:`send_text` for the full explanation — behaves identically
            here. Delivery is fire-and-forget (``InvocationType="Event"``) and
            non-fatal: failures are logged, never raised. Cannot be combined
            with ``log_to_memory`` — see Raises.

    Raises:
        ValueError: If immediate and log_to_memory are both True (an immediate
            mid-turn send must never write to memory — it would inject an
            assistant event between toolUse and toolResult and corrupt the
            session).
    """
    if immediate and log_to_memory:
        raise ValueError(
            "send_template: immediate=True cannot be combined with log_to_memory=True. "
            "An immediate send fires mid-turn; writing to AgentCore Memory there "
            "injects an assistant event between toolUse and toolResult and corrupts "
            "the session. Immediate sends are transient/UX-only."
        )

    q = _queue.get()
    if q is not None and not immediate:
        q.append(
            {
                "operation": "send_template",
                "phone": phone,
                "template_name": template_name,
                "params": params,
                "language": language,
                "log_to_memory": log_to_memory,
            }
        )
        return
    if config.is_dev_mode:
        _dev_inbox_append(
            {
                "type": "template",
                "phone": phone,
                "name": template_name,
                "params": params,
                "log_to_memory": log_to_memory,
            }
        )
        return
    payload = {
        "source": "socialagent",
        "operation": "send_template",
        "data": {
            "phone": phone,
            "template_name": template_name,
            "params": params,
            "language": language,
            "log_to_memory": log_to_memory,
        },
    }
    try:
        await asyncio.to_thread(
            _lambda_client().invoke,
            FunctionName=config.notification_lambda_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    except Exception:  # noqa: BLE001 — immediate UX sends are non-fatal
        if not immediate:
            raise
        logger.debug(
            "immediate send_template failed for %s", mask_phone(phone), exc_info=True
        )


async def send_message(
    phone: str,
    message: dict,
    *,
    log_to_memory: bool = False,
    memory_text: str = "",
    immediate: bool = False,
) -> None:
    """Send a rich message (interactive, image, etc.).

    Inside outbound_context(): queued for webhook delivery after the agent turn,
    always to the conversation's sender. Outside: sent immediately via
    notification Lambda.

    Args:
        phone: E.164 recipient phone number.
        message: WhatsApp message payload dict.
        log_to_memory: Write memory_text to AgentCore Memory after delivery.
            Requires memory_text to be set — raises ValueError otherwise, even
            for a queued send. Only writes memory outside outbound_context();
            a queued send never does.
        memory_text: Human-readable representation of the message for memory.
            Required when log_to_memory=True.
        immediate: Send right now instead of queuing for end-of-turn delivery.
            By default a send made inside ``outbound_context()`` (from a tool
            during an active turn) is queued and delivered by the webhook after
            the turn finishes. ``immediate=True`` dispatches it to the
            notification Lambda the instant you call it, even mid-tool — useful
            for pushing a rich card or media before a slow operation completes.
            It goes to ``phone`` as given, not pinned to the sender like a
            queued send, so never take ``phone`` from model input.
            Delivery is fire-and-forget (``InvocationType="Event"``) and
            non-fatal: failures are logged, never raised. Cannot be combined
            with ``log_to_memory`` — see Raises.

    Raises:
        ValueError: If immediate and log_to_memory are both True (an immediate
            mid-turn send must never write to memory — it would inject an
            assistant event between toolUse and toolResult and corrupt the
            session), or if log_to_memory is True without memory_text.
    """
    if immediate and log_to_memory:
        raise ValueError(
            "send_message: immediate=True cannot be combined with log_to_memory=True. "
            "An immediate send fires mid-turn; writing to AgentCore Memory there "
            "injects an assistant event between toolUse and toolResult and corrupts "
            "the session. Immediate sends are transient/UX-only."
        )
    if log_to_memory and not memory_text:
        raise ValueError(
            "memory_text is required when log_to_memory=True for send_message"
        )

    q = _queue.get()
    if q is not None and not immediate:
        q.append(
            {
                "operation": "send_message",
                "phone": phone,
                "message": message,
                "memory_text": memory_text if log_to_memory else "",
            }
        )
        return
    msg_type = message.get("type", "message")
    if config.is_dev_mode:
        _dev_inbox_append(
            {
                "type": "message",
                "phone": phone,
                "msg_type": msg_type,
                "message": message,
                "memory_text": memory_text,
                "log_to_memory": log_to_memory,
            }
        )
        return
    payload = {
        "source": "socialagent",
        "operation": "send_message",
        "data": {
            "phone": phone,
            "type": msg_type,
            "payload": message.get("payload", {}),
            "memory_text": memory_text if log_to_memory else "",
        },
    }
    try:
        await asyncio.to_thread(
            _lambda_client().invoke,
            FunctionName=config.notification_lambda_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    except Exception:  # noqa: BLE001 — immediate UX sends are non-fatal
        if not immediate:
            raise
        logger.debug(
            "immediate send_message failed for %s", mask_phone(phone), exc_info=True
        )


async def send_typing(phone: str, message_id: str) -> None:
    """Re-emit the WhatsApp "read receipt + typing indicator" for a conversation.

    On WhatsApp the "typing…" bubble (and the blue read ticks on the user's
    message) is produced by acknowledging a specific inbound message. The
    provider sends a single ``status: read`` + ``typing_indicator`` payload
    keyed off ``message_id``. The indicator is ephemeral — WhatsApp expires it
    after roughly 25 seconds — so a turn that runs longer than that goes silent
    and the bot looks dead.

    The webhook fires this once automatically the moment a message arrives,
    before the agent runs. That covers a fast turn, but not a slow one: if the
    agent then spends more than ~25s thinking or in a tool (image generation,
    an external API call, a file upload), the bubble disappears mid-turn. Call
    this to refresh it and keep the user seeing activity.

    Intended usage — rather than scattering calls through your tool code, wire
    it into a "before each tool runs" callback if your agent framework exposes
    one (most do), so the indicator is refreshed before every tool in a long
    turn::

        # whatever callback your framework runs before a tool executes
        async def before_tool_runs(...):
            await notify.send_typing(phone, message_id)

    Calling it directly inside a slow tool works too.

    This ALWAYS fires immediately, even inside ``outbound_context()``, because a
    typing indicator is order-independent and pointless if deferred to the end
    of the turn. It is always safe to call mid-tool: a typing indicator never
    writes to AgentCore Memory, so it cannot inject an assistant event between
    a toolUse and its toolResult (the corruption that the outbound queue exists
    to prevent for ordinary messages).

    Non-fatal: any failure is logged and swallowed — a typing indicator is a UX
    nicety and must never block the tool that requested it. No-op in dev mode
    and when ``phone`` or ``message_id`` is empty.

    Args:
        phone: E.164 recipient phone number — the user you are "typing" to.
        message_id: WhatsApp message id (``wamid``) of the inbound user message
            being acknowledged. WhatsApp attaches the read receipt and typing
            indicator to this id. Tools receive it on the invocation payload at
            ``payload["socialagent"]["message_id"]``.
    """
    if not phone or not message_id or config.is_dev_mode:
        return
    payload = {
        "source": "socialagent",
        "operation": "send_typing",
        "data": {"phone": phone, "message_id": message_id},
    }
    try:
        await asyncio.to_thread(
            _lambda_client().invoke,
            FunctionName=config.notification_lambda_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    except Exception:  # noqa: BLE001 — typing indicator failure is non-fatal
        logger.debug("send_typing failed", exc_info=True)


async def send_text(
    phone: str,
    text: str,
    *,
    log_to_memory: bool = False,
    immediate: bool = False,
) -> None:
    """Send a plain-text message.

    Inside outbound_context(): queued for webhook delivery after the agent turn,
    always to the conversation's sender. Outside: sent immediately via
    notification Lambda.

    Args:
        phone: E.164 recipient phone number.
        text: Message body.
        log_to_memory: Write the message text to AgentCore Memory after delivery.
            Only takes effect outside outbound_context(); a queued send ignores it.
        immediate: Send right now instead of queuing for end-of-turn delivery.

            By default (``immediate=False``), a send made from a tool during an
            active agent turn — i.e. inside ``outbound_context()`` — is appended
            to the outbound queue and delivered by the webhook only AFTER the
            turn finishes. That preserves message ordering and lets the
            framework's session manager record the turn cleanly.

            ``immediate=True`` opts out of that queue: the message is dispatched
            to the notification Lambda the instant you call it, even mid-tool,
            so the user sees it BEFORE a slow tool returns rather than bundled
            with its result at the end. Use it for progress / status updates
            such as "Generating your image, this may take a few seconds…".
            It goes to ``phone`` as given, not pinned to the sender like a
            queued send, so never take ``phone`` from model input.

            Delivery is fire-and-forget (``InvocationType="Event"``) and
            non-fatal: any failure is logged and swallowed, never raised, so a
            cosmetic progress message can never break the tool that sent it.
            (Outside ``outbound_context()`` the flag changes nothing — sends
            there already go out immediately.)

            Cannot be combined with ``log_to_memory`` — see Raises.

    Raises:
        ValueError: If immediate and log_to_memory are both True. An immediate
            send fires mid-turn (inside outbound_context); writing to AgentCore
            Memory there injects an assistant event between toolUse and
            toolResult and corrupts the session. Immediate sends are
            transient/UX-only and must never touch memory.
    """
    if immediate and log_to_memory:
        raise ValueError(
            "send_text: immediate=True cannot be combined with log_to_memory=True. "
            "An immediate send fires mid-turn; writing to AgentCore Memory there "
            "injects an assistant event between toolUse and toolResult and corrupts "
            "the session. Immediate sends are transient/UX-only."
        )

    q = _queue.get()
    if q is not None and not immediate:
        q.append(
            {
                "operation": "send_text",
                "phone": phone,
                "text": text,
                "memory_text": text if log_to_memory else "",
            }
        )
        return
    if config.is_dev_mode:
        _dev_inbox_append(
            {
                "type": "text",
                "phone": phone,
                "text": text,
                "log_to_memory": log_to_memory,
            }
        )
        return
    # Either outside a turn (safe to log_to_memory) or an immediate mid-turn send
    # (log_to_memory guaranteed False by the guard above → memory_text is "").
    payload = {
        "source": "socialagent",
        "operation": "send_text",
        "data": {
            "phone": phone,
            "text": text,
            "memory_text": text if log_to_memory else "",
        },
    }
    try:
        await asyncio.to_thread(
            _lambda_client().invoke,
            FunctionName=config.notification_lambda_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    except Exception:  # noqa: BLE001 — immediate UX sends are non-fatal
        if not immediate:
            raise
        logger.debug(
            "immediate send_text failed for %s", mask_phone(phone), exc_info=True
        )


async def upload_media(bucket: str, key: str) -> str:
    """Upload media from S3 to WhatsApp via the notification Lambda.

    Unlike the send_* helpers, this uses InvocationType="RequestResponse" so the
    WhatsApp media_id is returned synchronously — the calling agent tool needs the
    id before it returns. In dev mode, returns a stub media id and prints the
    request to stdout instead of invoking the Lambda.

    Args:
        bucket: S3 bucket containing the media file.
        key: S3 object key of the media file.

    Returns:
        The WhatsApp media_id string.
    """
    if config.is_dev_mode:
        print(f"[dev] upload_media: bucket={bucket!r}, key={key!r}")
        return "dev-media-id"
    payload = {
        "source": "socialagent",
        "operation": "upload_media",
        "data": {"bucket": bucket, "key": key},
    }
    resp = await asyncio.to_thread(
        _lambda_client().invoke,
        FunctionName=config.notification_lambda_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    # RequestResponse returns the handler's full return value as the payload.
    # The notification handler returns {"statusCode": 200, "body": json.dumps({"media_id": ...})},
    # so the outer payload's "body" is a JSON string we must parse again.
    #
    # Parse defensively: a Lambda fault, a non-2xx response, or a malformed
    # payload must surface as a clear error or an empty media id — never a
    # KeyError/JSONDecodeError. An absent media_id returns "" so the calling
    # tool's `if not media_id` guard can report a clean error to the agent.
    raw = resp.get("Payload")
    raw_bytes = raw.read() if raw is not None else b""

    # A handler exception surfaces as FunctionError with the traceback in the
    # payload — raise it so the failure is visible rather than silently empty.
    if resp.get("FunctionError"):
        detail = raw_bytes.decode("utf-8", "replace")[:500]
        raise RuntimeError(f"upload_media: notification Lambda error: {detail}")

    try:
        outer = json.loads(raw_bytes)
    except (json.JSONDecodeError, ValueError):
        logger.warning("upload_media: unparseable Lambda response payload")
        return ""

    body = outer.get("body") if isinstance(outer, dict) else None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            # Error responses carry a plain-string body (e.g. "unknown operation: ...").
            logger.warning(
                "upload_media: non-JSON body in Lambda response: %s", body[:200]
            )
            return ""
    elif body is None:
        # Fallback: media_id may sit directly on the outer payload.
        body = outer

    if not isinstance(body, dict):
        return ""
    return body.get("media_id", "")
