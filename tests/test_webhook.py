# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Webhook handler: payload building, handler flow,
before_invoke / after_invoke hooks, error mapping.
"""

import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.app import SocialAgentApp, _make_provider
from socialagent.config import SocialAgentConfig
from socialagent.constants import SOCIALAGENT_VERSION, DEFAULT_ERRORS
from socialagent.handlers.webhook import (
    _build_socialagent_payload,
    _extract_incoming_phone_id,
    _extract_response_text,
    _is_retryable,
    make_webhook_handler,
)
from socialagent.types import InboundMessage, MessageType, OutboundMessage, StatusUpdate
from socialagent.utils.phone import actor_id_for_phone, session_id_for_phone
from datetime import UTC, datetime


_TS = datetime.now(tz=UTC)


def _msg(text: str = "Hola", sender: str = "+525555550100") -> MagicMock:
    return MagicMock(id="wamid.1", sender=sender, type=MessageType.TEXT, text=text)


def _config(provider: str = "eum_social", **kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider=provider,
        phone_number_id="ph-x",
        **kwargs,
    )


def _provider(message: object = "DEFAULT") -> MagicMock:
    p = MagicMock()
    parsed = _msg() if message == "DEFAULT" else message
    p.parse_webhook = AsyncMock(return_value=parsed)
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    return p


# ── _build_socialagent_payload ───────────────────────────────────────────────────────


def test_build_socialagent_payload_structure() -> None:
    msg = InboundMessage(
        id="wamid.1",
        sender="+525555550100",
        timestamp=_TS,
        type=MessageType.TEXT,
        text="Hola",
    )
    config = _config()
    payload = _build_socialagent_payload(msg, config, "Hola")
    assert payload["prompt"] == "Hola"
    assert payload["phone"] == "+525555550100"
    socialagent = payload["socialagent"]
    assert socialagent["version"] == SOCIALAGENT_VERSION
    assert socialagent["memory_id"] == "mem-x"
    assert socialagent["actor_id"] == actor_id_for_phone("+525555550100")
    assert socialagent["session_id"] == session_id_for_phone("+525555550100")
    assert socialagent["message_id"] == "wamid.1"
    assert socialagent["message_type"] == "text"


def test_build_socialagent_payload_empty_text_becomes_empty_string() -> None:
    msg = InboundMessage(
        id="x", sender="+521", timestamp=_TS, type=MessageType.IMAGE, text=None
    )
    payload = _build_socialagent_payload(msg, _config(), "")
    assert payload["prompt"] == ""


# ── _is_retryable ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg", ["ThrottlingException", "ServiceUnavailable", "500", "503"]
)
def test_is_retryable_true(msg: str) -> None:
    assert _is_retryable(Exception(msg)) is True


def test_is_retryable_false() -> None:
    assert _is_retryable(Exception("ValidationException")) is False


# ── make_webhook_handler ──────────────────────────────────────────────────────


def test_handler_no_message_returns_200() -> None:
    provider = _provider(message=None)
    handler = make_webhook_handler(_config(), provider)
    result = handler({})
    assert result == {"statusCode": 200, "body": "no message"}
    provider.send_text.assert_not_called()


def test_handler_sends_plain_text_response() -> None:
    provider = _provider()
    with patch(
        "socialagent.handlers.webhook._invoke_runtime", return_value=("Respuesta", [])
    ):
        result = make_webhook_handler(_config(), provider)({"event": 1})
    assert result["statusCode"] == 200
    provider.send_text.assert_called_once_with("+525555550100", "Respuesta")


def test_handler_before_invoke_hook_modifies_payload() -> None:
    provider = _provider()
    captured = {}

    async def before(_message, payload):
        captured["payload"] = payload
        payload["extra"] = "injected"
        return payload

    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])):
        make_webhook_handler(_config(), provider, before_invoke=before)({"e": 1})

    assert captured["payload"]["extra"] == "injected"


def test_handler_after_invoke_hook_overrides_response() -> None:
    provider = _provider()

    async def after(_message, _response):
        return "Overridden"

    with patch(
        "socialagent.handlers.webhook._invoke_runtime", return_value=("Original", [])
    ):
        make_webhook_handler(_config(), provider, after_invoke=after)({"e": 1})

    provider.send_text.assert_called_once_with("+525555550100", "Overridden")


def test_handler_after_invoke_can_return_outbound_message() -> None:
    provider = _provider()
    rich = OutboundMessage(type="interactive", payload={})

    async def after(_message, _response):
        return rich

    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("x", [])):
        make_webhook_handler(_config(), provider, after_invoke=after)({"e": 1})

    provider.send_message.assert_called_once_with("+525555550100", rich)


def test_handler_sends_outbound_messages_before_text() -> None:
    """Queued outbound messages (attachments) are sent BEFORE the agent text response.

    This order matches the agent's reasoning sequence: tools run first (so
    attachments queue first during reasoning), final text comes after. Keeping
    the wire order aligned with the agent's order keeps memory, user UX, and
    the agent's mental model consistent. See docs/agent-integration.md.
    """
    provider = _provider()
    outbound = [
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {}},
            "memory_text": "",
        }
    ]
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=("Follow-up text", outbound),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})

    assert provider.send_message.call_count == 1
    assert provider.send_text.call_count == 1
    assert provider.send_message.call_args[0][1].type == "interactive"
    assert provider.send_text.call_args[0][1] == "Follow-up text"

    # Assert actual call order: send_message (attachment) BEFORE send_text (text response)
    call_names = [
        c[0] for c in provider.method_calls if c[0] in ("send_text", "send_message")
    ]
    assert call_names.index("send_message") < call_names.index("send_text"), (
        f"Expected send_message before send_text, got order: {call_names}"
    )


def test_handler_leaves_a_gap_before_the_text_after_attachments() -> None:
    """WhatsApp does not guarantee delivery order for messages sent back to back.

    Sent 16 ms apart, an interactive message and the text reply that follows it
    arrived inverted on a real phone, so the gap must also separate the last
    attachment from the text, not only attachments from each other.
    """
    import socialagent.handlers.webhook as webhook

    timeline: list[str] = []
    provider = _provider()
    provider.send_message = AsyncMock(
        side_effect=lambda *a, **k: timeline.append("message")
    )
    provider.send_text = AsyncMock(side_effect=lambda *a, **k: timeline.append("text"))

    async def fake_sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:g}")

    outbound = [
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {}},
        },
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {}},
        },
    ]
    with (
        patch.object(webhook, "_invoke_runtime", return_value=("Reply", outbound)),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=1.5), provider)({"e": 1})

    gap = "sleep 1.5"  # the configured value, not the default
    assert timeline == ["message", gap, "message", gap, "text"]


def test_handler_sends_plain_reply_without_a_gap() -> None:
    """With nothing queued, the reply is not delayed."""
    import socialagent.handlers.webhook as webhook

    provider = _provider()
    sleep = AsyncMock()
    with (
        patch.object(webhook, "_invoke_runtime", return_value=("Reply", [])),
        patch.object(webhook.asyncio, "sleep", sleep),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})

    sleep.assert_not_awaited()
    provider.send_text.assert_awaited_once()


def _runtime_returning(body: dict) -> MagicMock:
    """A stand-in boto3 module whose AgentCore client answers with ``body``."""
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value.invoke_agent_runtime.return_value = {
        "response": io.BytesIO(json.dumps(body).encode())
    }
    return fake_boto3


_BUTTONS = {
    "operation": "send_message",
    "message": {"type": "interactive", "payload": {"interactive": {}}},
}


@pytest.mark.parametrize("result", ["", "\n", "   "])
def test_handler_attachments_only_reply_sends_no_error(result: str) -> None:
    """A turn that queues attachments and ends with no text is a full reply.

    The rich-messages skill tells the model the closing text is optional, and
    a Strands result with no text block stringifies to "". The attachments go
    out and nothing follows them: no error text, and no gap before a send that
    never happens.
    """
    import socialagent.handlers.webhook as webhook

    timeline: list[str] = []
    provider = _provider()
    provider.send_message = AsyncMock(
        side_effect=lambda *a, **k: timeline.append("message")
    )
    provider.send_text = AsyncMock(side_effect=lambda *a, **k: timeline.append("text"))

    async def fake_sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:g}")

    body = {"result": result, "outbound_messages": [_BUTTONS, _BUTTONS]}
    with (
        patch("socialagent.handlers.runtime_call.boto3", _runtime_returning(body)),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=1.5), provider)({"e": 1})

    assert timeline == ["message", "sleep 1.5", "message"]


def test_handler_empty_reply_with_nothing_queued_sends_generic_error() -> None:
    """With no text and nothing queued the user would get nothing at all."""
    provider = _provider()
    body = {"result": "", "outbound_messages": []}
    with patch("socialagent.handlers.runtime_call.boto3", _runtime_returning(body)):
        make_webhook_handler(_config(), provider)({"e": 1})

    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["generic"]
    )


def test_handler_attachments_only_reply_sends_error_when_none_delivered() -> None:
    """If every queued message fails to send, the user still hears back."""
    provider = _provider()
    provider.send_message = AsyncMock(side_effect=RuntimeError("rejected"))
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=(None, [_BUTTONS]),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=0), provider)({"e": 1})

    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["generic"]
    )


_S3_IMAGE = {
    "operation": "send_message",
    "message": {
        "type": "image",
        "payload": {
            "image": {
                "link": "https://my-stack-media.s3.us-east-1.amazonaws.com/images/a.png"
            }
        },
    },
}


@pytest.mark.parametrize(
    "upload_result",
    [{"side_effect": RuntimeError("upload failed")}, {"return_value": ""}],
    ids=["failed", "refused"],
)
def test_handler_attachments_only_reply_with_an_s3_link_left_sends_error(
    upload_result: dict,
) -> None:
    """An S3 link that was not turned into a media id does not reach the user.

    WhatsApp cannot fetch an object in the stack's private bucket, so the image
    sent by its link never arrives, though the send itself succeeds. Counted as
    delivered, it left a turn with no text answered by nothing at all.
    """
    provider = _provider()
    upload = AsyncMock(**upload_result)
    provider.upload_media_from_s3 = upload
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=(None, [_S3_IMAGE]),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=0), provider)({"e": 1})

    upload.assert_awaited_once_with("my-stack-media", "images/a.png")
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["generic"]
    )


def test_handler_attachments_only_reply_with_an_uploaded_image_sends_no_error() -> None:
    provider = _provider()
    provider.upload_media_from_s3 = AsyncMock(return_value="media-1")
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=(None, [_S3_IMAGE]),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=0), provider)({"e": 1})

    (call,) = provider.send_message.call_args_list
    assert call.args[1].payload == {"image": {"id": "media-1"}}
    provider.send_text.assert_not_called()


def test_handler_attachments_only_reply_gives_after_invoke_empty_text() -> None:
    """after_invoke sees the empty reply, not an error, and may still add text."""
    provider = _provider()
    seen: list[object] = []

    async def after(_message, response):
        seen.append(response)
        return "Pick one"

    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=(None, [_BUTTONS]),
    ):
        make_webhook_handler(
            _config(outbound_gap_seconds=0), provider, after_invoke=after
        )({"e": 1})

    assert seen == [""]
    provider.send_message.assert_called_once()
    provider.send_text.assert_called_once_with("+525555550100", "Pick one")


def test_handler_outbound_text_message() -> None:
    """Queued send_text outbound messages are delivered."""
    provider = _provider()
    outbound = [
        {"operation": "send_text", "text": "Hello from tool", "memory_text": ""}
    ]
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=("Agent reply", outbound),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})

    assert provider.send_text.call_count == 2  # outbound + agent reply
    calls = [c[0][1] for c in provider.send_text.call_args_list]
    assert "Hello from tool" in calls
    assert "Agent reply" in calls


def test_handler_pins_queued_recipient_to_sender(caplog) -> None:
    """A queued message goes to the sender, never to an agent-supplied phone.

    A prompt-injected agent must not be able to redirect an in-band reply to an
    arbitrary number, so a differing ``phone`` is logged and ignored. Restoring a
    ``msg["phone"]`` fallback in the drain fails this test.
    """
    provider = _provider()
    outbound = [
        {"operation": "send_text", "phone": "+15555550199", "text": "For the admin"},
        {
            "operation": "send_message",
            "phone": "+15555550199",
            "message": {"type": "interactive", "payload": {}},
        },
    ]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=("Agent reply", outbound),
        ),
        caplog.at_level(logging.WARNING, logger="socialagent.handlers.webhook"),
    ):
        make_webhook_handler(_config(outbound_gap_seconds=0), provider)({"e": 1})

    sends = provider.send_text.call_args_list + provider.send_message.call_args_list
    assert len(sends) == 3  # both queued messages + the agent reply
    assert {c[0][0] for c in sends} == {"+525555550100"}
    pinned = [r for r in caplog.records if "pinning to sender" in r.getMessage()]
    assert len(pinned) == 2


def test_handler_throttle_error_returns_error_message() -> None:
    provider = _provider()
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        side_effect=Exception("ThrottlingException"),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})
    text = provider.send_text.call_args[0][1]
    assert text == DEFAULT_ERRORS["throttle"]


def test_handler_generic_error_returns_error_message() -> None:
    provider = _provider()
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        side_effect=Exception("InternalServerError"),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})
    text = provider.send_text.call_args[0][1]
    assert len(text) > 0


def test_handler_custom_error_messages() -> None:
    provider = _provider()
    config = _config(error_messages={"generic": "Custom error"})
    with patch(
        "socialagent.handlers.webhook._invoke_runtime", side_effect=Exception("boom")
    ):
        make_webhook_handler(config, provider)({"e": 1})
    provider.send_text.assert_called_once_with("+525555550100", "Custom error")


def test_handler_sends_typing_indicator() -> None:
    provider = _provider()
    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])):
        make_webhook_handler(_config(), provider)({"e": 1})
    provider.send_typing.assert_called_once_with("+525555550100", "wamid.1")


# ── SocialAgentApp ───────────────────────────────────────────────────────────────────


def test_socialagent_app_unknown_provider_raises() -> None:
    config = _config(provider="unknown_provider")
    with pytest.raises(ValueError, match="unknown_provider"):
        _make_provider(config)


def test_socialagent_app_hook_decorators_registered() -> None:
    with (
        patch("socialagent.app._make_provider"),
        patch("socialagent.app.SocialAgentMemoryWriter"),
    ):
        app = SocialAgentApp(_config())

    @app.before_invoke
    async def before(_m, p):
        return p

    @app.after_invoke
    async def after(_m, r):
        return r

    @app.on_event("order_created")
    async def on_order(e, ctx):
        pass

    @app.on_schedule("reminder")
    async def on_reminder(e, ctx):
        pass

    assert app._before_invoke is before
    assert app._after_invoke is after
    assert "order_created" in app._event_handlers
    assert "reminder" in app._schedule_handlers


# ── on_status hook ────────────────────────────────────────────────────────────


def _status_provider(status: StatusUpdate) -> MagicMock:
    """Provider that returns a StatusUpdate from parse_webhook."""
    p = MagicMock()
    p.parse_webhook = AsyncMock(return_value=status)
    p.send_text = AsyncMock()
    return p


def _make_status() -> StatusUpdate:
    return StatusUpdate(
        message_id="wamid.1",
        recipient="+525555550100",
        status="delivered",
        timestamp=datetime.now(UTC),
    )


def test_handler_status_update_calls_on_status_hook() -> None:
    status = _make_status()
    provider = _status_provider(status)
    received = []

    async def on_status(s: StatusUpdate) -> None:
        received.append(s)

    handler = make_webhook_handler(_config(), provider, on_status=on_status)
    result = handler({})
    assert result == {"statusCode": 200, "body": "ok"}
    assert received == [status]
    provider.send_text.assert_not_called()


def test_handler_status_update_no_hook_returns_ok() -> None:
    provider = _status_provider(_make_status())
    handler = make_webhook_handler(_config(), provider)
    result = handler({})
    assert result == {"statusCode": 200, "body": "ok"}
    provider.send_text.assert_not_called()


# ── SOCIALAGENT_META_PHONE_NUMBER_ID phone filter (fail-closed) ───────────────────────


def _sns_event(phone_id: str) -> dict:
    """Build an SNS-wrapped webhook event carrying a Meta phone_number_id."""
    inner = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_id},
                            "messages": [{"from": "525555550100", "type": "text"}],
                        }
                    }
                ]
            }
        ]
    }
    return {
        "Records": [{"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": inner})}}]
    }


def test_extract_incoming_phone_id_nested_entry() -> None:
    assert _extract_incoming_phone_id(_sns_event("PHONE_A")) == "PHONE_A"


def test_extract_incoming_phone_id_missing_returns_empty() -> None:
    assert _extract_incoming_phone_id({"Records": [{"Sns": {"Message": "{}"}}]}) == ""
    assert (
        _extract_incoming_phone_id({"Records": [{"Sns": {"Message": "not json"}}]})
        == ""
    )
    assert _extract_incoming_phone_id({}) == ""


def test_filter_processes_matching_phone() -> None:
    """Event whose phone id matches the configured filter is processed."""
    provider = _provider()
    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])):
        result = make_webhook_handler(
            _config(meta_phone_number_id="PHONE_A"), provider
        )(_sns_event("PHONE_A"))
    assert result == {"statusCode": 200, "body": "ok"}
    provider.send_text.assert_called_once()


def test_filter_drops_mismatched_phone() -> None:
    """Event for a sibling stack's phone is dropped."""
    provider = _provider()
    result = make_webhook_handler(_config(meta_phone_number_id="PHONE_A"), provider)(
        _sns_event("PHONE_B")
    )
    assert result == {"statusCode": 200, "body": "not for us"}
    provider.parse_webhook.assert_not_called()
    provider.send_text.assert_not_called()


def test_filter_fails_closed_on_unparseable_phone() -> None:
    """Fail closed: an SNS event with no parseable phone id is dropped, not processed."""
    provider = _provider()
    event = {"Records": [{"Sns": {"Message": "{}"}}]}
    result = make_webhook_handler(_config(meta_phone_number_id="PHONE_A"), provider)(
        event
    )
    assert result == {"statusCode": 200, "body": "not for us"}
    provider.parse_webhook.assert_not_called()


def test_filter_disabled_passes_through() -> None:
    """With no filter configured, events pass through regardless of phone id."""
    provider = _provider()
    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])):
        result = make_webhook_handler(_config(), provider)(_sns_event("ANY"))
    assert result == {"statusCode": 200, "body": "ok"}


def test_filter_ignores_non_sns_events() -> None:
    """A direct (non-Records) invocation is not subject to the SNS phone filter."""
    provider = _provider()
    with patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])):
        result = make_webhook_handler(
            _config(meta_phone_number_id="PHONE_A"), provider
        )({"event": 1})
    assert result == {"statusCode": 200, "body": "ok"}


# ── Robustness: prompt resolution + outbound drain loop ─────────────────────────


def test_handler_prompt_resolution_failure_sends_generic_error() -> None:
    """If media/prompt resolution raises, the user still gets the generic error."""
    media_msg = MagicMock(
        id="wamid.1", sender="+525555550100", type=MessageType.IMAGE, text=None
    )
    provider = _provider(message=media_msg)

    async def failing_media(_message):
        raise RuntimeError("transcription exploded")

    handler = make_webhook_handler(_config(), provider, on_media=failing_media)
    with patch("socialagent.handlers.webhook._invoke_runtime") as inv:
        result = handler({"e": 1})
    assert result == {"statusCode": 200, "body": "ok"}
    inv.assert_not_called()  # never reached the runtime
    sent = provider.send_text.call_args[0][1]
    assert sent == DEFAULT_ERRORS["generic"]


def test_handler_malformed_outbound_does_not_abort_text_reply() -> None:
    """A non-dict queued message is skipped without aborting the agent's text reply."""
    provider = _provider()
    outbound = ["not-a-dict", {"operation": "send_text", "text": "queued"}]
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=("Agent reply", outbound),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})
    sent_texts = [c[0][1] for c in provider.send_text.call_args_list]
    assert "queued" in sent_texts
    assert "Agent reply" in sent_texts


# ── runtime response extraction: never leak internals to the user ──────────────


def test_extract_response_text_prefers_result() -> None:
    assert _extract_response_text({"result": "hola"}) == "hola"


def test_extract_response_text_falls_back_to_response_key() -> None:
    assert _extract_response_text({"response": "hola"}) == "hola"


def test_extract_response_text_none_for_error_dict() -> None:
    # A bare error payload must NOT be stringified and delivered to the user.
    assert _extract_response_text({"error": "KeyError: 'phone'"}) is None


def test_extract_response_text_none_for_null_result() -> None:
    # {"result": null} used to become the literal string "None".
    assert _extract_response_text({"result": None}) is None


def test_extract_response_text_none_for_non_dict() -> None:
    # A bare JSON string/list is not a usable reply shape.
    assert _extract_response_text("just a string") is None
    assert _extract_response_text(["a", "b"]) is None


def test_extract_response_text_none_for_blank() -> None:
    assert _extract_response_text({"result": "   "}) is None


@pytest.mark.asyncio
async def test_queued_messages_with_empty_text_are_a_complete_reply():
    """The agent may answer with a menu or list and no text. That is the reply;
    the generic error must not follow it, and no blank text is sent."""
    from socialagent.handlers.webhook import _invoke_and_respond

    provider = MagicMock()
    provider.send_text = AsyncMock()
    provider.send_message = AsyncMock()
    config = _config()
    outbound = [
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {"type": "list"}},
        }
    ]
    with (
        patch(
            "socialagent.handlers.webhook._call_runtime",
            AsyncMock(return_value=(None, outbound)),
        ),
        patch(
            "socialagent.handlers.webhook._upload_media_if_needed",
            AsyncMock(side_effect=lambda m, p: m),
        ),
    ):
        await _invoke_and_respond(
            config,
            provider,
            "+525555550100",
            {"prompt": "hola"},
            {"generic": "Sorry, I had a problem processing your message."},
            None,
            _msg(),
        )
    provider.send_message.assert_awaited_once()
    provider.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_text_and_no_outbound_does_not_leave_silently():
    """A turn that produced no reply at all must not return without a word.

    The queued-messages path sets an empty response on purpose, so the blank
    check skips the send. A runtime that returns an empty string instead of
    ``None`` reaches the same check without having queued anything: the user
    would get nothing and no log would say so. The send is attempted, and
    ``_send_response`` logs the rejection.
    """
    from socialagent.handlers.webhook import _invoke_and_respond

    provider = MagicMock()
    provider.send_text = AsyncMock()
    provider.send_message = AsyncMock()
    with patch(
        "socialagent.handlers.webhook._call_runtime", AsyncMock(return_value=("", []))
    ):
        await _invoke_and_respond(
            _config(),
            provider,
            "+525555550100",
            {"prompt": "hola"},
            {"generic": "Sorry, I had a problem processing your message."},
            None,
            _msg(),
        )
    provider.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_text_and_no_outbound_still_sends_the_generic_error():
    from socialagent.handlers.webhook import _invoke_and_respond

    provider = MagicMock()
    provider.send_text = AsyncMock()
    provider.send_message = AsyncMock()
    with patch(
        "socialagent.handlers.webhook._call_runtime", AsyncMock(return_value=(None, []))
    ):
        await _invoke_and_respond(
            _config(),
            provider,
            "+525555550100",
            {"prompt": "hola"},
            {"generic": "Sorry, I had a problem processing your message."},
            None,
            _msg(),
        )
    provider.send_text.assert_awaited_once()
    assert "Sorry" in provider.send_text.await_args.args[1]
