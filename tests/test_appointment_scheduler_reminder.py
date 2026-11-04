# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The appointment-scheduler's visit reminder survives a throttled send.

Lambda does not retry the scheduler Lambda (``maximum_retry_attempts = 0``), so
a reminder whose send fails after the AWS SDK's own retries goes to the DLQ and
the user gets nothing. Reminders due at the same minute fire together, so a
burst can be throttled. The handler tries a throttled send again itself, one
send at a time, so a pin that is throttled does not send the template twice.
Any other error is raised at once.

The real EventContext sends before it writes memory and swallows a failed
write, so a throttled memory write never reaches the retry. The tests at the
end run the wrapper on it, since the ``_Ctx`` stub sends and writes in one call
and cannot show that ordering.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from socialagent.handlers.event import EventContext
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.types import OutboundMessage

REPO_ROOT = Path(__file__).resolve().parents[1]
PHONE = "+15555550100"
BOOKING = {
    "phone": PHONE,
    "property": "Garden House",
    "time": "10:00 AM",
    "location": "48 Elm Ave",
    "location_name": "Garden House",
    "lat": 19.4178,
    "lng": -99.1838,
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "SendWhatsAppMessage")


def _blueprint_scheduler(monkeypatch):
    """The blueprint's handlers/scheduler.py, with asyncio.sleep recording its
    pauses instead of waiting."""
    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
    }.items():
        monkeypatch.setenv(k, v)
    # No __pycache__ in the blueprint, which `socialagent init` copies.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    handlers_dir = REPO_ROOT / "blueprints" / "appointment-scheduler" / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_reminder_retry_probe", handlers_dir / "scheduler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)
    pauses: list[float] = []

    async def no_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod, pauses


def _visit_reminder_handler(monkeypatch):
    mod, pauses = _blueprint_scheduler(monkeypatch)
    return mod.app._schedule_handlers["visit_reminder"], pauses


class _NoopState:
    async def delete(self, phone, key):
        return None

    async def put(self, phone, key, data, ttl_minutes=None):
        return None


class _Ctx:
    """Stands in for EventContext, state included, and fails the first sends
    of each kind with the given error codes."""

    state = _NoopState()

    def __init__(self, template_errors=(), message_errors=()):
        self.template_errors = list(template_errors)
        self.message_errors = list(message_errors)
        self.attempts: list[str] = []
        self.sent: list[str] = []

    async def send_template(self, phone, name, params, language="en"):
        self.attempts.append("template")
        if self.template_errors:
            raise _client_error(self.template_errors.pop(0))
        self.sent.append("template")

    async def send_message(self, phone, msg, memory_text=""):
        self.attempts.append("location")
        if self.message_errors:
            raise _client_error(self.message_errors.pop(0))
        self.sent.append("location")

    async def write_to_memory(self, phone, text):
        self.sent.append("memory")


@pytest.mark.parametrize("code", ["ThrottledRequestException", "ThrottlingException"])
def test_a_throttled_reminder_is_tried_again_and_sent_once(monkeypatch, code):
    """EUM Social names its throttle ThrottledRequestException."""
    handler, pauses = _visit_reminder_handler(monkeypatch)
    ctx = _Ctx(template_errors=[code])
    asyncio.run(handler(dict(BOOKING), ctx))
    assert ctx.attempts == ["template", "template", "location"]
    assert ctx.sent == ["template", "location", "memory"]
    # The 10 s before the pin, after one 2 s pause for the throttled send.
    assert pauses == [2, 10]


def test_a_throttled_pin_does_not_send_the_template_again(monkeypatch):
    handler, _ = _visit_reminder_handler(monkeypatch)
    ctx = _Ctx(message_errors=["ThrottledRequestException"])
    asyncio.run(handler(dict(BOOKING), ctx))
    assert ctx.attempts == ["template", "location", "location"]
    assert ctx.sent == ["template", "location", "memory"]


def test_a_reminder_throttled_three_times_fails_the_run(monkeypatch):
    """It goes to the DLQ after three tries, not after one."""
    handler, pauses = _visit_reminder_handler(monkeypatch)
    ctx = _Ctx(template_errors=["ThrottledRequestException"] * 3)
    with pytest.raises(ClientError):
        asyncio.run(handler(dict(BOOKING), ctx))
    assert ctx.attempts == ["template"] * 3
    assert ctx.sent == []
    assert pauses == [2, 4]


@pytest.mark.parametrize(
    "error",
    [_client_error("ValidationException"), RuntimeError("template not approved")],
)
def test_an_error_that_is_not_a_throttle_is_raised_at_once(monkeypatch, error):
    """A rejected send fails the same way when tried again, so it is not."""
    handler, pauses = _visit_reminder_handler(monkeypatch)

    class RejectingCtx(_Ctx):
        async def send_template(self, phone, name, params, language="en"):
            self.attempts.append("template")
            raise error

    ctx = RejectingCtx()
    with pytest.raises(type(error)):
        asyncio.run(handler(dict(BOOKING), ctx))
    assert ctx.attempts == ["template"]
    assert pauses == []


def test_the_delivery_marker_is_written_after_a_throttled_template_goes_out(
    monkeypatch,
):
    """``at`` is a delivery: the marker waits for the send that succeeded, and
    precedes the location pin, whose ten-second pause is when a quick reply runs."""
    handler, _ = _visit_reminder_handler(monkeypatch)
    log: list[str] = []

    class LoggingState(_NoopState):
        async def put(self, phone, key, data, ttl_minutes=None):
            log.append(f"put:{key}")

    class LoggingCtx(_Ctx):
        state = LoggingState()

        async def send_template(self, phone, name, params, language="en"):
            log.append("template")
            await super().send_template(phone, name, params, language)

        async def send_message(self, phone, msg, memory_text=""):
            log.append("location")
            await super().send_message(phone, msg, memory_text)

    ctx = LoggingCtx(template_errors=["ThrottledRequestException"])
    asyncio.run(handler(dict(BOOKING), ctx))
    assert log == ["template", "template", "put:reminder_sent", "location"]


def test_a_state_failure_before_the_send_does_not_cost_the_reminder(
    monkeypatch, caplog
):
    """The scheduler Lambda is not retried, so a failed delete of the open cancel
    question must not stop the reminder."""
    handler, _ = _visit_reminder_handler(monkeypatch)

    class BrokenState(_NoopState):
        async def delete(self, phone, key):
            raise RuntimeError("state unavailable")

    class BrokenCtx(_Ctx):
        state = BrokenState()

    ctx = BrokenCtx()
    with caplog.at_level("WARNING"):
        asyncio.run(handler(dict(BOOKING), ctx))
    assert ctx.sent[0] == "template"
    assert "could not clear the open cancel question" in caplog.text


@pytest.mark.parametrize("code", ["ThrottledRequestException", "ThrottlingException"])
def test_a_throttled_memory_write_after_a_send_does_not_send_again(monkeypatch, code):
    """The real EventContext sends, then writes memory, and log_delivered
    swallows a failed write. So a throttled write after a delivered send never
    reaches the wrapper's retry, which would send the reminder again."""
    handler, pauses = _visit_reminder_handler(monkeypatch)
    provider = AsyncMock()
    memory = AsyncMock()
    memory.log_assistant_message.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "x"}}, "CreateEvent"
    )
    ctx = EventContext(provider, memory, registry=None, state=_NoopState())
    asyncio.run(handler(dict(BOOKING), ctx))
    provider.send_template.assert_awaited_once()
    provider.send_message.assert_awaited_once()
    # The template, the pin and the handler's own note, each written once.
    assert memory.log_assistant_message.await_count == 3
    assert pauses == [10]


class _SocialMessaging:
    """Stands in for the socialmessaging client: records what each call
    delivered, and throttles the calls numbered in ``throttle`` (from 1)."""

    def __init__(self, throttle=()):
        self.calls = 0
        self.throttle = set(throttle)
        self.delivered: list[str] = []

    def send_whatsapp_message(self, **kwargs):
        self.calls += 1
        if self.calls in self.throttle:
            raise _client_error("ThrottledRequestException")
        self.delivered.append(json.loads(kwargs["message"])["text"]["body"])
        return {"messageId": f"m{self.calls}"}


def test_a_throttled_chunk_of_a_long_text_does_not_resend_the_ones_before_it(
    monkeypatch,
):
    """The provider sends a long text in chunks, so retrying the whole
    send_message resent the first chunk when the second was throttled."""
    mod, pauses = _blueprint_scheduler(monkeypatch)
    chunks = [letter * 1500 + "\n\n" for letter in "ABC"] + ["D" * 1500]
    client = _SocialMessaging(throttle={2})
    provider = EUMSocialProvider("phone-id", message_gap_seconds=0)
    provider._client = client
    memory = AsyncMock()
    ctx = mod._ThrottleRetryContext(
        EventContext(provider, memory, registry=None, state=_NoopState())
    )
    message = OutboundMessage(type="text", payload={"text": {"body": "".join(chunks)}})
    asyncio.run(ctx.send_message(PHONE, message))
    # By first letter first, so a failure reads ['A', 'A', 'B', ...].
    assert [body[0] for body in client.delivered] == list("ABCD")
    assert client.delivered == chunks
    # Memory gets each chunk as it is sent: EventContext has no send that
    # leaves memory out.
    written = [c.args[1] for c in memory.log_assistant_message.await_args_list]
    assert written == chunks
    # The gap between chunks stays, with the 2 s pause before chunk 2's retry.
    gap = mod.config.outbound_gap_seconds
    assert pauses == [gap, 2, gap, gap]


def test_a_short_text_message_is_tried_again_as_one_send(monkeypatch):
    mod, pauses = _blueprint_scheduler(monkeypatch)
    client = _SocialMessaging(throttle={1})
    provider = EUMSocialProvider("phone-id", message_gap_seconds=0)
    provider._client = client
    memory = AsyncMock()
    ctx = mod._ThrottleRetryContext(
        EventContext(provider, memory, registry=None, state=_NoopState())
    )
    message = OutboundMessage(type="text", payload={"text": {"body": "See you at 10"}})
    asyncio.run(ctx.send_message(PHONE, message, memory_text="[Reminder text]"))
    assert client.delivered == ["See you at 10"]
    memory.log_assistant_message.assert_awaited_once_with(PHONE, "[Reminder text]")
    assert pauses == [2]
