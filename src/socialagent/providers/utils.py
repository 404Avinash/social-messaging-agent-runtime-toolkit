# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared utilities for Meta (WhatsApp) providers.

Used by EUMSocialProvider.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from socialagent.constants import MAX_MSG_LEN, MEDIA_TYPES
from socialagent.types import (
    ContactContent,
    InboundMessage,
    LocationContent,
    MediaContent,
    MessageType,
    ReactionContent,
    StatusUpdate,
)
from socialagent.utils.phone import normalize_phone

logger = logging.getLogger(__name__)

# Maps Meta webhook message type strings to MessageType enum values.
# "interactive" is resolved separately via interactive.type.
TYPE_MAP: dict[str, MessageType | None] = {
    "text": MessageType.TEXT,
    "image": MessageType.IMAGE,
    "audio": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "document": MessageType.DOCUMENT,
    "location": MessageType.LOCATION,
    "contacts": MessageType.CONTACT,
    "reaction": MessageType.REACTION,
    "button": MessageType.BUTTON_REPLY,
    "interactive": None,
    "sticker": MessageType.STICKER,
}

# interactive.type -> MessageType. ``nfm_reply`` is a WhatsApp Flow or address
# form submission. Unknown interactive types fall back to LIST_REPLY.
_INTERACTIVE_TYPES: dict[str, MessageType] = {
    "button_reply": MessageType.BUTTON_REPLY,
    "list_reply": MessageType.LIST_REPLY,
    "nfm_reply": MessageType.FORM_REPLY,
}

REPLY_TYPES = frozenset(
    {MessageType.BUTTON_REPLY, MessageType.LIST_REPLY, MessageType.FORM_REPLY}
)
"""Message types that answer an interactive message the bot sent."""


def reply_prompt(kind: MessageType, title: str, description: str = "") -> str:
    """The agent prompt for a reply to an interactive message.

    The bare title (``Comedia``) reads like free text, and the agent's restored
    history does not show the interactive message it answers (tool calls are
    stripped on restore). The tag says it was a click on an option the bot sent.
    """
    label = {
        MessageType.BUTTON_REPLY: "Button reply",
        MessageType.LIST_REPLY: "List reply",
        MessageType.FORM_REPLY: "Form reply",
    }[kind]
    detail = f" — {description}" if description else ""
    return f"[{label}: {title}{detail}]"


def interactive_reply_prompt(msg: dict, kind: MessageType) -> str | None:
    """Build :func:`reply_prompt` from a raw Meta message dict, or None.

    Covers reply buttons (``interactive.button_reply``), template quick-reply
    buttons (``button``), list rows (``interactive.list_reply``, with the row
    description) and form submissions (``interactive.nfm_reply``, whose answers
    are in ``response_json``).
    """
    interactive = msg.get("interactive", {}) or {}
    if kind == MessageType.BUTTON_REPLY:
        title = (interactive.get("button_reply") or {}).get("title") or (
            msg.get("button") or {}
        ).get("text")
        return reply_prompt(kind, title) if title else None
    if kind == MessageType.LIST_REPLY:
        row = interactive.get("list_reply") or {}
        title = row.get("title")
        return (
            reply_prompt(kind, title, row.get("description") or "") if title else None
        )
    if kind == MessageType.FORM_REPLY:
        form = interactive.get("nfm_reply") or {}
        answer = form.get("response_json") or form.get("body") or ""
        name = form.get("name") or "form"
        return reply_prompt(kind, name, str(answer)[:1000]) if answer else None
    return None


def reaction_prompt(reaction: ReactionContent | None) -> str:
    """The agent prompt for an emoji reaction to an earlier message.

    Carries the emoji, so the agent can tell a thumbs-up from a thumbs-down, and
    the wamid it was added to. A removed reaction arrives with no emoji; the
    webhook runs no turn for one.
    """
    if reaction is None:
        return "[User sent a reaction]"
    target = reaction.reacted_message_id
    if not reaction.emoji:
        return f"[User removed their reaction to message {target}]"
    return f"[User reacted {reaction.emoji} to message {target}]"


# ── Webhook parsing ───────────────────────────────────────────────────────────


def extract_changes(entry: dict) -> dict | None:
    """Extract the ``changes[0].value`` dict from a Meta webhook entry.

    Handles both the standard ``entry[0].changes[0].value`` shape and the
    EUM Social variant that omits the outer ``entry`` wrapper.
    Returns ``None`` if the payload cannot be parsed.
    """
    try:
        return entry["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError):
        pass
    try:
        return entry["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        # The keys only: the entry carries the sender's number, name and text.
        logger.warning(
            "Could not extract changes from entry (keys=%s)",
            sorted(entry)[:10] if isinstance(entry, dict) else type(entry).__name__,
        )
        return None


def parse_status(status: dict) -> StatusUpdate | None:
    """Convert a Meta status dict into a :class:`StatusUpdate`."""
    if "id" not in status:
        return None
    return StatusUpdate(
        message_id=status["id"],
        recipient="+" + status.get("recipient_id", "").lstrip("+"),
        status=status.get("status", ""),
        timestamp=datetime.fromtimestamp(int(status.get("timestamp", 0)), tz=UTC),
        raw=status,
    )


def parse_message(msg: dict) -> InboundMessage:
    """Convert a single Meta message dict into an :class:`InboundMessage`."""
    sender = normalize_phone(msg["from"])
    ts = datetime.fromtimestamp(int(msg["timestamp"]), tz=UTC)
    raw_type = msg.get("type", "text")

    if raw_type == "interactive":
        msg_type = _INTERACTIVE_TYPES.get(
            msg.get("interactive", {}).get("type", ""), MessageType.LIST_REPLY
        )
    else:
        msg_type = TYPE_MAP.get(raw_type, MessageType.TEXT)

    context = msg.get("context", {})
    return InboundMessage(
        id=msg["id"],
        sender=sender,
        timestamp=ts,
        type=msg_type,
        text=_extract_text(msg, msg_type),
        media=_extract_media(msg),
        location=_extract_location(msg),
        contacts=_extract_contacts(msg),
        reaction=_extract_reaction(msg),
        reply_to=context.get("id"),
        forwarded=context.get("forwarded", False),
        raw=msg,
    )


def _extract_text(msg: dict, msg_type: MessageType) -> str | None:
    if msg_type == MessageType.TEXT:
        return msg.get("text", {}).get("body")
    if msg_type == MessageType.BUTTON_REPLY:
        title = msg.get("interactive", {}).get("button_reply", {}).get("title")
        if title:
            return title
        return msg.get("button", {}).get("text")
    if msg_type == MessageType.LIST_REPLY:
        return msg.get("interactive", {}).get("list_reply", {}).get("title")
    if msg_type == MessageType.FORM_REPLY:
        return msg.get("interactive", {}).get("nfm_reply", {}).get("body")
    return None


def _extract_media(msg: dict) -> MediaContent | None:
    for media_type in MEDIA_TYPES:
        if media_type in msg:
            m = msg[media_type]
            return MediaContent(
                id=m.get("id", ""),
                mime_type=m.get("mime_type", ""),
                sha256=m.get("sha256"),
                caption=m.get("caption"),
                filename=m.get("filename"),
            )
    return None


def _extract_location(msg: dict) -> LocationContent | None:
    if "location" not in msg:
        return None
    loc = msg["location"]
    return LocationContent(
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        name=loc.get("name"),
        address=loc.get("address"),
    )


def _extract_contacts(msg: dict) -> list[ContactContent] | None:
    if "contacts" not in msg:
        return None
    return [
        ContactContent(
            name=c.get("name", {}).get("formatted_name", ""),
            phones=[p["phone"] for p in c.get("phones", [])],
        )
        for c in msg["contacts"]
    ]


def _extract_reaction(msg: dict) -> ReactionContent | None:
    if "reaction" not in msg:
        return None
    r = msg["reaction"]
    # A removed reaction may leave the emoji out; that must not fail the parse.
    return ReactionContent(
        emoji=r.get("emoji") or "", reacted_message_id=r["message_id"]
    )


# ── Text utilities ────────────────────────────────────────────────────────────


def chunk_text(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split text into chunks of at most ``limit`` chars, breaking at natural boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + limit
        if end < len(text):
            for sep in ("\n\n", "\n", ". ", " "):
                idx = text.rfind(sep, start, end)
                if idx > start:
                    end = idx + len(sep)
                    break
        chunks.append(text[start:end])
        start = end
    return chunks


def sanitize_template_param(value: object) -> str:
    """Coerce a template parameter to a newline-free string.

    Coerces first: callers routinely pass numbers straight from a JSON event body
    (an order id, an amount), and ``value.replace(...)`` on a non-str would raise
    AttributeError, which is non-retryable. Meta silently drops messages
    containing newlines, so strip those too.
    """
    return str(value).replace("\n", " ").replace("\r", " ")
