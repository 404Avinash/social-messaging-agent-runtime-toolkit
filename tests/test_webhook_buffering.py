# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the message-buffer wiring in the webhook handler + the SQS enqueue helper.

The buffer engine itself is covered by test_message_buffer.py. These tests isolate
the webhook's routing decisions by mocking the MessageBuffer and the runtime call:

- text inbound (buffering on) → enqueue delayed flush, NO runtime invoke
- non-text inbound (buffering on) → bypass buffer → immediate runtime invoke
- buffering off → immediate runtime invoke
- SQS flush event → combine + invoke + clear
- superseded SQS flush → no invoke, no clear
- send_flush helper → SQS send_message with DelaySeconds (and no-op when unconfigured)

All AWS calls are mocked — fully offline.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.buffer import FlushDecision, IngestResult
from socialagent.config import SocialAgentConfig
from socialagent.constants import DEFAULT_ERRORS
from socialagent.handlers.webhook import (
    _is_sqs_event,
    _synth_flush_message,
    make_webhook_handler,
)
from socialagent.types import MessageType


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        **kwargs,
    )


def _text_msg(text: str = "hola", sender: str = "+525555550100") -> MagicMock:
    return MagicMock(id="wamid.1", sender=sender, type=MessageType.TEXT, text=text)


def _provider(parsed: object = "DEFAULT") -> MagicMock:
    p = MagicMock()
    p.parse_webhook = AsyncMock(
        return_value=_text_msg() if parsed == "DEFAULT" else parsed
    )
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    return p


def _fake_buffer(
    *, enabled: bool, ingest_seq: int = 1, flush: FlushDecision | None = None
) -> MagicMock:
    buf = MagicMock()
    buf.is_enabled = MagicMock(return_value=enabled)
    buf.ingest_text = AsyncMock(
        return_value=IngestResult(seq=ingest_seq, flush_now=False)
    )
    buf.should_flush = AsyncMock(return_value=flush)
    buf.clear = AsyncMock()
    buf.clear_flushed = AsyncMock()
    return buf


# ── inbound text with buffering ON → enqueue, no runtime invoke ────────────────


def test_text_inbound_buffers_and_enqueues_without_invoking() -> None:
    cfg = _config(
        message_buffering_enabled=True, buffer_window_seconds=7, _state_table_name="t"
    )
    provider = _provider()
    fake = _fake_buffer(enabled=True, ingest_seq=3)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()) as send_flush,
        patch("socialagent.handlers.webhook._invoke_runtime") as invoke,
    ):
        handler = make_webhook_handler(cfg, provider)
        result = handler({})  # SNS-shaped (provider.parse_webhook mocked)

    assert result == {"statusCode": 200, "body": "buffered"}
    fake.ingest_text.assert_awaited_once()
    send_flush.assert_awaited_once_with(cfg, "+525555550100", 3)
    invoke.assert_not_called()  # runtime NOT invoked on the buffering path


def test_text_inbound_is_marked_read_before_it_is_buffered() -> None:
    """Each buffered message gets its read receipt + typing indicator at ingest.

    The buffering path returns before the agent runs, so the only chance to turn
    the user's ticks blue is the ``send_typing`` call the webhook makes before
    ingesting the text; the flush later carries only the combined prompt.
    """
    cfg = _config(
        message_buffering_enabled=True, buffer_window_seconds=7, _state_table_name="t"
    )
    provider = _provider()
    fake = _fake_buffer(enabled=True, ingest_seq=3)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()),
        patch("socialagent.handlers.webhook._invoke_runtime"),
    ):
        make_webhook_handler(cfg, provider)({})

    provider.send_typing.assert_awaited_once_with("+525555550100", "wamid.1")


@pytest.mark.parametrize("failing", ["ingest", "enqueue"])
def test_text_that_cannot_be_buffered_gets_the_generic_error(failing: str) -> None:
    """Lambda does not retry the SNS event, so an exception here meant silence.

    The text is neither answered nor scheduled; the user is told, and can send
    it again. A text stored before the enqueue failed goes out with that one.
    """
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    provider = _provider()
    fake = _fake_buffer(enabled=True)
    send_flush = AsyncMock()
    if failing == "ingest":
        fake.ingest_text = AsyncMock(side_effect=RuntimeError("dynamodb unavailable"))
    else:
        send_flush.side_effect = RuntimeError("sqs unavailable")

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook.send_flush", new=send_flush),
        patch("socialagent.handlers.webhook._invoke_runtime") as invoke,
    ):
        result = make_webhook_handler(cfg, provider)({})

    assert result["statusCode"] == 200
    invoke.assert_not_called()
    provider.send_text.assert_awaited_once_with(
        "+525555550100", DEFAULT_ERRORS["generic"]
    )


# ── non-text inbound bypasses the buffer even when enabled ─────────────────────


def test_non_text_inbound_bypasses_buffer() -> None:
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    image_msg = MagicMock(
        id="wamid.img", sender="+525555550100", type=MessageType.IMAGE, text=None
    )
    provider = _provider(parsed=image_msg)
    fake = _fake_buffer(enabled=True)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()) as send_flush,
        patch(
            "socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])
        ) as invoke,
    ):
        handler = make_webhook_handler(
            cfg, provider, on_media=AsyncMock(return_value="img prompt")
        )
        result = handler({})

    assert result == {"statusCode": 200, "body": "ok"}
    send_flush.assert_not_awaited()
    fake.ingest_text.assert_not_awaited()
    invoke.assert_called_once()  # immediate path


# ── buffering OFF → immediate path ─────────────────────────────────────────────


def test_buffering_disabled_invokes_immediately() -> None:
    cfg = _config(message_buffering_enabled=False)
    provider = _provider()
    fake = _fake_buffer(enabled=False)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()) as send_flush,
        patch(
            "socialagent.handlers.webhook._invoke_runtime", return_value=("hi", [])
        ) as invoke,
    ):
        handler = make_webhook_handler(cfg, provider)
        result = handler({})

    assert result == {"statusCode": 200, "body": "ok"}
    send_flush.assert_not_awaited()
    invoke.assert_called_once()
    provider.send_text.assert_awaited()  # response delivered


# ── SQS flush event → combine + invoke + clear ─────────────────────────────────


def _sqs_event(phone: str = "+525555550100", seq: int = 2) -> dict:
    return {
        "Records": [
            {"eventSource": "aws:sqs", "body": json.dumps({"phone": phone, "seq": seq})}
        ]
    }


def test_sqs_flush_invokes_and_clears() -> None:
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    provider = _provider()
    decision = FlushDecision(
        True,
        "window_elapsed",
        [
            {"type": "text", "text": "hola", "message_id": "wamid.1"},
            {"type": "text", "text": "quiero info", "message_id": "wamid.2"},
        ],
    )
    fake = _fake_buffer(enabled=True, flush=decision)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=("respuesta", []),
        ) as invoke,
    ):
        handler = make_webhook_handler(cfg, provider)
        result = handler(_sqs_event(seq=2))

    assert result == {"statusCode": 200, "body": "ok"}
    fake.should_flush.assert_awaited_once()
    invoke.assert_called_once()
    # combined prompt is the newline-joined buffered texts
    sent_payload = invoke.call_args.args[2]
    assert sent_payload["prompt"] == "hola\nquiero info"
    provider.send_text.assert_awaited_with("+525555550100", "respuesta")
    # The snapshot it consumed, not the whole partition — a message that arrived
    # during the invoke must survive for the next flush.
    fake.clear_flushed.assert_awaited_once_with("+525555550100", decision.messages)
    fake.clear.assert_not_awaited()


# ── superseded SQS flush → no invoke, no clear ─────────────────────────────────


def test_sqs_flush_superseded_is_noop() -> None:
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    provider = _provider()
    fake = _fake_buffer(enabled=True, flush=FlushDecision(False, "superseded", []))

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch("socialagent.handlers.webhook._invoke_runtime") as invoke,
    ):
        handler = make_webhook_handler(cfg, provider)
        result = handler(_sqs_event())

    assert result == {"statusCode": 200, "body": "ok"}
    invoke.assert_not_called()
    fake.clear.assert_not_awaited()
    provider.send_text.assert_not_awaited()


def test_sqs_flush_clears_even_if_invoke_raises() -> None:
    """A hard failure during the flush still clears the buffer (try/finally), so
    SQS at-least-once redelivery can't re-invoke and double-reply."""
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    provider = _provider()
    decision = FlushDecision(
        True,
        "window_elapsed",
        [{"type": "text", "text": "hi", "message_id": "wamid.1"}],
    )
    fake = _fake_buffer(enabled=True, flush=decision)

    with (
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake),
        patch(
            "socialagent.handlers.webhook._invoke_and_respond",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        handler = make_webhook_handler(cfg, provider)
        with pytest.raises(RuntimeError, match="boom"):
            handler(_sqs_event())

    # The snapshot it consumed, not the whole partition — a message that arrived
    # during the invoke must survive for the next flush.
    fake.clear_flushed.assert_awaited_once_with("+525555550100", decision.messages)
    fake.clear.assert_not_awaited()


# ── send_flush helper ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_flush_enqueues_with_delay() -> None:
    from socialagent.buffer import queue

    cfg = _config(buffer_window_seconds=8, _buffer_queue_url="https://sqs.example/q")
    mock_client = MagicMock()
    with patch.object(queue, "_sqs_client", return_value=mock_client):
        await queue.send_flush(cfg, "+525555550100", 5)

    mock_client.send_message.assert_called_once()
    kwargs = mock_client.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs.example/q"
    assert kwargs["DelaySeconds"] == 8
    assert json.loads(kwargs["MessageBody"]) == {"phone": "+525555550100", "seq": 5}


@pytest.mark.asyncio
async def test_send_flush_noop_without_queue_url() -> None:
    from socialagent.buffer import queue

    cfg = _config(_buffer_queue_url="")
    mock_client = MagicMock()
    with patch.object(queue, "_sqs_client", return_value=mock_client):
        await queue.send_flush(cfg, "+525555550100", 1)
    mock_client.send_message.assert_not_called()


# ── helper units: _is_sqs_event + _synth_flush_message ─────────────────────────


def test_is_sqs_event_discriminates_source() -> None:
    assert (
        _is_sqs_event({"Records": [{"eventSource": "aws:sqs", "body": "{}"}]}) is True
    )
    assert _is_sqs_event({"Records": [{"EventSource": "aws:sns", "Sns": {}}]}) is False
    assert _is_sqs_event({}) is False
    assert _is_sqs_event({"operation": "health_check"}) is False


def test_synth_flush_message_carries_last_id_and_combined_text() -> None:
    msgs = [
        {"type": "text", "text": "a", "message_id": "m1"},
        {"type": "text", "text": "b", "message_id": "m2"},
    ]
    m = _synth_flush_message("+525555550100", msgs, "a\nb")
    assert m.id == "m2"  # last buffered message's wamid
    assert m.sender == "+525555550100"
    assert m.type == MessageType.TEXT
    assert m.text == "a\nb"
    assert m.timestamp is not None
