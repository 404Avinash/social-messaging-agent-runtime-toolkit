# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Restored-history filter shared by the Strands session managers.

Conversation history can outlive the model and system prompt that produced it.
Some Converse blocks are bound to the context they were produced in, and a
model rejects them when they are replayed after that context changed. They are
therefore dropped on restore by their block type, never by model ID.

A bot that answers through tools (``attach_interactive``, ``send_text``) has
assistant turns made only of a reasoning block and a ``toolUse`` block. Dropping
both would drop the turn, and the restored history would be the user talking to
no one, so the model re-asks what it already asked. Such a turn is kept as the
text the user actually saw, taken from the tool input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

# Converse block types that must not be replayed. A content block is a dict
# whose single top-level key is its type; add a newly introduced
# context-bound type here rather than branching on model IDs.
# A thinking block is rejected once earlier history changed; an image or
# document block (from attachments.build_prompt) would resend the file on every
# later turn, against Memory's event size limit; a toolUse/toolResult pair can
# straddle the restore window, and Bedrock rejects a toolResult whose toolUse is
# missing ("toolResult blocks exceeds toolUse blocks of previous turn"). The
# text block that accompanies any of them stays, so nothing said is lost.
REPLAY_UNSAFE_BLOCKS = frozenset(
    {"reasoningContent", "image", "document", "toolUse", "toolResult"}
)

# Tool input fields that hold the text the user saw. Checked in order; the
# first non-empty one stands in for the assistant turn on restore. Recipes and
# the runtime's own helpers all name their user-facing text one of these.
_VISIBLE_TEXT_FIELDS = ("body", "text", "message", "caption")


def _visible_text(block: dict) -> str:
    """The user-facing text a ``toolUse`` block carried, or ``""``.

    Internal tools (``load_skill``, ``execute_action``) have none: their turn
    said nothing to the user and may drop.
    """
    tool_input = block.get("toolUse", {}).get("input")
    if not isinstance(tool_input, dict):
        return ""
    for field in _VISIBLE_TEXT_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _delivered_tool_uses(messages: list[Any]) -> set[str]:
    """Ids of the ``toolUse`` blocks whose ``toolResult`` reports success.

    The assistant message is persisted before its tool runs, so a toolUse
    whose result is an error, or has no result at all (the turn raised), is in
    the history although the user received nothing. Only a delivered one may
    stand in as text; the others drop with the turn.

    "Delivered" is as far as the history can see: a successful result means
    the tool queued its message. If the turn raises after that, the webhook
    drops the queue and the user gets only the error text, yet this turn
    still restores as the queued text. Nothing persisted records the drop.
    """
    delivered: set[str] = set()
    for session_message in messages:
        for block in session_message.to_message().get("content", []):
            result = block.get("toolResult") if isinstance(block, dict) else None
            if result and result.get("status", "success") != "error":
                delivered.add(result.get("toolUseId", ""))
    return delivered


def _replay_safe_content(content: list[Any], delivered: set[str]) -> list[Any]:
    """Keep the safe blocks; if none remain, stand in the delivered tools' text."""
    kept = [
        block
        for block in content
        if not (isinstance(block, dict) and not REPLAY_UNSAFE_BLOCKS.isdisjoint(block))
    ]
    if kept:
        return kept
    texts = [
        t
        for t in (
            _visible_text(b)
            for b in content
            if isinstance(b, dict)
            and b.get("toolUse", {}).get("toolUseId", "") in delivered
        )
        if t
    ]
    return [{"text": "\n\n".join(texts)}] if texts else []


def strip_replay_unsafe(messages: list[Any]) -> list[Any]:
    """Return *messages* without ``REPLAY_UNSAFE_BLOCKS`` blocks.

    The stored messages are never mutated: an unchanged message is returned
    as is, a changed one as a copy. A tool-only assistant turn becomes the
    text its tool showed the user; a turn left with nothing (an internal
    tool call, or a tool result) is dropped. The filter reads the effective
    message (``to_message()`` prefers ``redact_message``), and a copy clears
    ``redact_message`` so a redacted form cannot bring the dropped blocks back.
    A tool-only turn stands in as text only when its tool result reports
    success: the assistant message is persisted before the tool runs, so a
    failed or unfinished one never reached the user.
    """
    delivered = _delivered_tool_uses(messages)
    kept_messages = []
    for session_message in messages:
        effective = session_message.to_message()
        content = effective.get("content", [])
        kept = _replay_safe_content(content, delivered)
        if kept == content:
            kept_messages.append(session_message)
        elif kept:
            kept_messages.append(
                replace(
                    session_message,
                    message={**effective, "content": kept},
                    redact_message=None,
                )
            )
    return _merge_same_role_runs(kept_messages)


def _merge_same_role_runs(messages: list[Any]) -> list[Any]:
    """Collapse consecutive same-role messages so the history alternates.

    Dropping a turn can leave two user (or two assistant) messages adjacent,
    which Converse rejects. The later message's text blocks are appended to
    the earlier one; the earlier message's identity is kept.
    """
    merged: list[Any] = []
    for session_message in messages:
        message = session_message.to_message()
        if merged and merged[-1].to_message()["role"] == message["role"]:
            previous = merged[-1].to_message()
            merged[-1] = replace(
                merged[-1],
                message={
                    **previous,
                    "content": previous["content"] + message["content"],
                },
                redact_message=None,
            )
        else:
            merged.append(session_message)
    return merged


def list_replay_safe(
    list_messages: Callable[..., list[Any]],
    session_id: str,
    agent_id: str,
    limit: int | None = None,
    offset: int = 0,
    **kwargs: Any,
) -> list[Any]:
    """Page through the history *list_messages* returns, after filtering it.

    Filtering can drop whole messages, so paging the raw history first would
    leave a page short. The full history is fetched and filtered, then
    sliced; both session managers already fetch everything on restore.
    """
    messages = strip_replay_unsafe(
        list_messages(
            session_id=session_id, agent_id=agent_id, limit=None, offset=0, **kwargs
        )
    )
    end = None if limit is None else offset + limit
    return messages[offset:end]
