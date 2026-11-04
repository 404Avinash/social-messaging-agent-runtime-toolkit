# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for immediate (mid-turn) notification delivery.

Covers the elegant replacement of the earlier ``send_text_now`` / ``send_typing_now``
helpers with:

- ``notify.send_text(..., immediate=True)`` / ``notify.send_message(..., immediate=True)``
  / ``notify.send_template(..., immediate=True)`` — bypass the outbound queue even
  inside ``outbound_context()`` and fire-and-forget via the notification Lambda.
  Non-fatal.
- ``notify.send_typing(phone, message_id)`` — intrinsically immediate, never writes
  to memory.
- The notification handler ``send_typing`` route → ``provider.send_typing``.

The central invariant under test: an immediate send must NEVER write to memory
(``immediate=True`` + ``log_to_memory=True`` raises), because a mid-turn memory
write injects an assistant event between toolUse and toolResult and corrupts the
AgentCore Memory session.

All AWS calls are mocked — these run fully offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.handlers.notification import make_notification_handler

# packages/socialagent-runtime is a dash-named dir wired in via sys.path (matches the
# rest of the suite, e.g. test_notify_upload_media.py).
_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import notify as notify_mod  # noqa: E402


_PROD_ARN = "arn:aws:lambda:::function:notify"


def _prod_arn() -> None:
    object.__setattr__(notify_mod.config, "notification_lambda_arn", _PROD_ARN)


def _reset_arn() -> None:
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
    )


# ── notification handler: send_typing route ────────────────────────────────────


def test_handler_routes_send_typing() -> None:
    """send_typing operation calls provider.send_typing and never writes memory."""
    provider = MagicMock()
    provider.send_typing = AsyncMock()
    memory = MagicMock()
    memory.log_assistant_message = AsyncMock()

    handler = make_notification_handler(provider, memory)
    result = handler(
        {"operation": "send_typing", "data": {"phone": "+521", "message_id": "wamid.1"}}
    )

    assert result["statusCode"] == 200
    provider.send_typing.assert_awaited_once_with("+521", "wamid.1")
    memory.log_assistant_message.assert_not_called()


# ── invariant: immediate sends must never write to memory ──────────────────────


@pytest.mark.asyncio
async def test_send_text_immediate_with_log_to_memory_raises() -> None:
    """immediate=True + log_to_memory=True is a contradiction → ValueError."""
    with pytest.raises(ValueError, match="immediate=True cannot be combined"):
        await notify_mod.send_text("+521", "hi", immediate=True, log_to_memory=True)


@pytest.mark.asyncio
async def test_send_message_immediate_with_log_to_memory_raises() -> None:
    with pytest.raises(ValueError, match="immediate=True cannot be combined"):
        await notify_mod.send_message(
            "+521", {"type": "text"}, immediate=True, log_to_memory=True
        )


@pytest.mark.asyncio
async def test_send_template_immediate_with_log_to_memory_raises() -> None:
    with pytest.raises(ValueError, match="immediate=True cannot be combined"):
        await notify_mod.send_template(
            "+521", "welcome", ["Ana"], immediate=True, log_to_memory=True
        )


# ── immediate=True bypasses the queue even inside outbound_context ──────────────


@pytest.mark.asyncio
async def test_send_text_immediate_bypasses_queue_inside_context() -> None:
    """Inside outbound_context, immediate=True sends now instead of queuing,
    and forces memory_text='' (no memory write)."""
    _prod_arn()
    try:
        mock_client = MagicMock()
        with notify_mod.outbound_context() as queue:
            with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
                await notify_mod.send_text("+521", "Processing…", immediate=True)
            # Nothing was queued — it fired immediately.
            assert queue == []

        mock_client.invoke.assert_called_once()
        kwargs = mock_client.invoke.call_args[1]
        assert kwargs["InvocationType"] == "Event"
        payload = json.loads(kwargs["Payload"])
        assert payload["operation"] == "send_text"
        assert payload["data"]["memory_text"] == ""
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_text_non_immediate_still_queues_inside_context() -> None:
    """Regression: default (immediate=False) still queues inside the context."""
    mock_client = MagicMock()
    with notify_mod.outbound_context() as queue:
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_text("+521", "deferred")
        assert len(queue) == 1
        assert queue[0]["operation"] == "send_text"
    mock_client.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_immediate_bypasses_queue_inside_context() -> None:
    _prod_arn()
    try:
        mock_client = MagicMock()
        with notify_mod.outbound_context() as queue:
            with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
                await notify_mod.send_message(
                    "+521", {"type": "text", "payload": {}}, immediate=True
                )
            assert queue == []
        mock_client.invoke.assert_called_once()
        assert mock_client.invoke.call_args[1]["InvocationType"] == "Event"
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_template_immediate_bypasses_queue_inside_context() -> None:
    _prod_arn()
    try:
        mock_client = MagicMock()
        with notify_mod.outbound_context() as queue:
            with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
                await notify_mod.send_template(
                    "+521", "welcome", ["Ana"], immediate=True
                )
            assert queue == []
        mock_client.invoke.assert_called_once()
        kwargs = mock_client.invoke.call_args[1]
        assert kwargs["InvocationType"] == "Event"
        payload = json.loads(kwargs["Payload"])
        assert payload["operation"] == "send_template"
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_template_non_immediate_still_queues_inside_context() -> None:
    """Regression: default (immediate=False) still queues inside the context."""
    mock_client = MagicMock()
    with notify_mod.outbound_context() as queue:
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_template("+521", "welcome", ["Ana"])
        assert len(queue) == 1
        assert queue[0]["operation"] == "send_template"
    mock_client.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_send_template_language_is_keyword_only() -> None:
    """language must not be passable positionally — prevents silently setting
    it to a truthy non-string value (e.g. a misplaced log_to_memory=True)."""
    with pytest.raises(TypeError):
        await notify_mod.send_template("+521", "welcome", ["Ana"], "en_US")  # type: ignore[misc]


# ── immediate sends are non-fatal ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_text_immediate_swallows_errors() -> None:
    """A Lambda failure during an immediate send is logged, never raised."""
    _prod_arn()
    try:
        mock_client = MagicMock()
        mock_client.invoke.side_effect = RuntimeError("lambda down")
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            # Should NOT raise.
            await notify_mod.send_text("+521", "warn", immediate=True)
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_template_immediate_swallows_errors() -> None:
    """A Lambda failure during an immediate template send is logged, never raised."""
    _prod_arn()
    try:
        mock_client = MagicMock()
        mock_client.invoke.side_effect = RuntimeError("lambda down")
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_template("+521", "welcome", ["Ana"], immediate=True)
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_text_non_immediate_propagates_errors() -> None:
    """Outside a turn, a non-immediate send still surfaces failures."""
    _prod_arn()
    try:
        mock_client = MagicMock()
        mock_client.invoke.side_effect = RuntimeError("lambda down")
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="lambda down"):
                await notify_mod.send_text("+521", "important")
    finally:
        _reset_arn()


# ── send_typing helper ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_typing_invokes_lambda_with_send_typing_op() -> None:
    _prod_arn()
    try:
        mock_client = MagicMock()
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_typing("+521", "wamid.1")
        mock_client.invoke.assert_called_once()
        kwargs = mock_client.invoke.call_args[1]
        assert kwargs["InvocationType"] == "Event"
        payload = json.loads(kwargs["Payload"])
        assert payload["operation"] == "send_typing"
        assert payload["data"] == {"phone": "+521", "message_id": "wamid.1"}
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_typing_noop_in_dev_mode() -> None:
    """Empty ARN => dev mode => no Lambda invocation."""
    _reset_arn()  # the sentinel is what enables dev mode
    mock_client = MagicMock()
    with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
        await notify_mod.send_typing("+521", "wamid.1")
    mock_client.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_send_typing_noop_on_missing_args() -> None:
    _prod_arn()
    try:
        mock_client = MagicMock()
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_typing("", "wamid.1")
            await notify_mod.send_typing("+521", "")
        mock_client.invoke.assert_not_called()
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_send_typing_swallows_errors() -> None:
    _prod_arn()
    try:
        mock_client = MagicMock()
        mock_client.invoke.side_effect = RuntimeError("boom")
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            await notify_mod.send_typing("+521", "wamid.1")  # must not raise
    finally:
        _reset_arn()
