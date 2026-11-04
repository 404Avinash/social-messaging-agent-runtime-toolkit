# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What both Lambdas do around a schedule's built-in send.

A schedule's ``send_text``, ``send_template`` or ``send_message`` runs on the
scheduler Lambda or, on a stack without one, on the notification Lambda. With
the state table on, neither gets a Lambda retry for it: the scheduler Lambda
has them off, and on the notification Lambda the firing record, kept when the
send fails, turns Lambda's retry into a ``duplicate``. So the handler's own
retry (``handlers/scheduled.py``) is the only one the send gets, and it must
know EUM Social's errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from moto import mock_aws

from socialagent.app import SocialAgentApp
from socialagent.config import SocialAgentConfig
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduled import is_retryable
from socialagent.handlers.scheduler_event import make_scheduler_handler
from socialagent.state.table import SocialAgentState

TABLE = "dev-bot-state"
PHONE = "+525555550100"
DIGITS = "525555550100"
NINE = "2026-10-30T09:00:00Z"
LAMBDAS = ["scheduler", "notification"]

# SendWhatsAppMessage's errors in botocore's model: a throttle (429), a fault at
# EUM Social (500) or at Meta (502), and the refusals (400, 403, 404).
TRANSIENT = [
    "ThrottledRequestException",
    "InternalServiceException",
    "DependencyException",
]
REFUSED = [
    "ValidationException",
    "InvalidParametersException",
    "AccessDeniedException",
    "ResourceNotFoundException",
]


def _eum_error(code: str) -> ClientError:
    """What SendWhatsAppMessage raises once the AWS SDK's own retries are spent."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": "Rate exceeded"},
            "ResponseMetadata": {"MaxAttemptsReached": True, "RetryAttempts": 4},
        },
        "SendWhatsAppMessage",
    )


def _config(table_name: str = TABLE) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:aws:bedrock-agentcore:::runtime/x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-id",
        region="us-east-1",
        _state_table_name=table_name,
    )


@pytest.fixture
def table(monkeypatch):
    """A state table with the stack's key schema, in moto."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield SocialAgentState(_config())


@pytest.fixture
def provider():
    p = AsyncMock()
    p.send_text = AsyncMock(return_value="m1")
    p.send_message = AsyncMock(return_value="m2")
    p.send_template = AsyncMock(return_value="m3")
    return p


def _handler(which: str, provider, state):
    if which == "scheduler":
        return make_scheduler_handler(provider, AsyncMock(), {}, state=state)
    return make_notification_handler(provider, AsyncMock(), state=state)


def _scheduled(op: str, **data) -> dict:
    """A schedule's payload as EventBridge Scheduler delivers it."""
    return {
        "source": "socialagent",
        "operation": op,
        "data": {"phone": PHONE, **data},
        "schedule_id": f"{DIGITS}-{op}-abcd1234",
        "scheduled_time": NINE,
    }


def _deliver(handler, event: dict) -> dict:
    # Each delivery is a fresh copy of the payload, as Lambda hands it over.
    return handler(json.loads(json.dumps(event)))


# ── Which errors are tried again ──────────────────────────────────────────────


@pytest.mark.parametrize("code", [*TRANSIENT, "ThrottlingException"])
def test_a_transient_client_error_is_retryable(code):
    """Read from the error code: the ClientError's text, ``An error occurred
    (ThrottledRequestException) when calling the SendWhatsAppMessage
    operation``, names neither a status nor a generic marker."""
    assert is_retryable(_eum_error(code)) is True


@pytest.mark.parametrize("code", REFUSED)
def test_a_refused_send_is_not_retryable(code):
    """EUM Social refused it, so the same send would be refused again."""
    assert is_retryable(_eum_error(code)) is False


URL = "https://social-messaging.us-east-1.amazonaws.com/"
# What botocore raises once its own retries are spent. A request that never
# connected never reached EUM Social; one that timed out reading, or whose
# connection closed, may have been sent.
NEVER_SENT = [EndpointConnectionError, ConnectTimeoutError]
MAYBE_SENT = [ReadTimeoutError, ConnectionClosedError]


@pytest.mark.parametrize("error", NEVER_SENT, ids=lambda e: e.__name__)
def test_a_send_that_never_connected_is_retryable(error):
    """Not a ClientError, and its text has none of the markers."""
    assert is_retryable(error(endpoint_url=URL)) is True


@pytest.mark.parametrize("error", MAYBE_SENT, ids=lambda e: e.__name__)
def test_a_send_that_may_have_gone_out_is_not_retryable(error):
    """Trying it again could send it twice."""
    assert is_retryable(error(endpoint_url=URL)) is False


# ── Through the handler, as the Lambda runs it ────────────────────────────────


@pytest.mark.parametrize("code", TRANSIENT)
@pytest.mark.parametrize("which", LAMBDAS)
def test_a_scheduled_send_eum_social_fails_once_is_sent(table, provider, which, code):
    """The handler's retry is the only one the send gets: a second delivery,
    Lambda's retry on the notification Lambda included, finds the record."""
    provider.send_template = AsyncMock(side_effect=[_eum_error(code), "m3"])
    handler = _handler(which, provider, table)
    event = _scheduled("send_template", template_name="reminder_clean", params=[])
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        assert _deliver(handler, event) == {"statusCode": 200, "body": "ok"}
        assert _deliver(handler, event) == {"statusCode": 200, "body": "duplicate"}
    assert provider.send_template.await_count == 2


@pytest.mark.parametrize("error", NEVER_SENT, ids=lambda e: e.__name__)
@pytest.mark.parametrize("which", LAMBDAS)
def test_a_scheduled_send_that_never_connected_is_sent(table, provider, which, error):
    """On the notification Lambda, Lambda's two retries delivered it before the
    firing record. Now they find the record, so the handler's retry must take
    the error."""
    never = error(endpoint_url=URL)
    provider.send_text = AsyncMock(side_effect=[never, never, "m1"])
    handler = _handler(which, provider, table)
    event = _scheduled("send_text", text="Hola")
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        # The first invocation, and the two retries Lambda (or EventBridge)
        # would make after a failure.
        bodies = [_deliver(handler, event)["body"] for _ in range(3)]
    assert bodies == ["ok", "duplicate", "duplicate"]
    assert provider.send_text.await_count == 3


# ── A long text, paced as WhatsApp needs ─────────────────────────────────────

# Two WhatsApp messages: a chunk of "A"s, then one of "B"s.
LONG = "A" * 1600 + "B" * 200


class _LambdaContext:
    """A Lambda context with ``seconds`` left before the timeout."""

    def __init__(self, seconds: float) -> None:
        self._millis = int(seconds * 1000)

    def get_remaining_time_in_millis(self) -> int:
        return self._millis


@pytest.fixture
def wire(monkeypatch):
    """Each send and pause, in order: ("send", first letter) or ("sleep", s)."""
    events: list[tuple[str, object]] = []

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return events


def _app_handler(
    which: str, wire: list, *, gap: float = 1.5, fail_on=None, table_name=TABLE
):
    """The handler as the Lambda builds it, from a stack whose gap is ``gap``.

    The provider is the real EUMSocialProvider, with only its call to EUM
    Social replaced. ``fail_on(chunk, attempt)`` raises for a chunk's attempt.
    """
    config = _config(table_name)
    config.outbound_gap_seconds = gap
    app = SocialAgentApp(config)
    app.memory_writer = AsyncMock()
    attempts: dict[str, int] = {}

    def send_raw(payload: dict) -> str:
        body = payload["text"]["body"]
        attempts[body] = attempts.get(body, 0) + 1
        wire.append(("send", body[0]))
        if fail_on is not None:
            fail_on(body, attempts[body])
        return "wamid"

    app.provider._send_raw = send_raw
    if which == "scheduler":
        return app.scheduler_handler()
    return app.notification_handler()


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_long_scheduled_text_pauses_between_its_chunks(table, wire, which):
    """WhatsApp can deliver messages sent back to back out of order, so a split
    reminder would arrive scrambled. The chunks go out outbound_gap_seconds
    apart, as ctx.send_text and a send from notify pace them."""
    handler = _app_handler(which, wire)
    assert _deliver(handler, _scheduled("send_text", text=LONG))["body"] == "ok"
    assert wire == [("send", "A"), ("sleep", 1.5), ("send", "B")]


def test_a_long_unrecorded_scheduled_text_pauses_between_its_chunks(table, wire):
    """Without the table the notification Lambda records nothing, so it sends
    as for notify, through the provider, which paces the chunks itself. (The
    fixture keeps AWS mocked; this stack's config names no table.)"""
    handler = _app_handler("notification", wire, table_name="")
    assert _deliver(handler, _scheduled("send_text", text=LONG))["body"] == "ok"
    assert wire == [("send", "A"), ("sleep", 1.5), ("send", "B")]


def test_a_long_unrecorded_scheduled_text_keeps_the_pause_near_the_timeout(table, wire):
    """The exception the docs name: unrecorded, the send goes through
    the provider, which pauses the full gap whatever time is left."""
    handler = _app_handler("notification", wire, table_name="")
    event = json.loads(json.dumps(_scheduled("send_text", text=LONG)))
    assert handler(event, _LambdaContext(5))["body"] == "ok"
    assert wire == [("send", "A"), ("sleep", 1.5), ("send", "B")]


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_long_scheduled_text_drops_the_pause_near_the_timeout(table, wire, which):
    """Out of order beats the Lambda timing out with the rest unsent."""
    handler = _app_handler(which, wire)
    event = json.loads(json.dumps(_scheduled("send_text", text=LONG)))
    assert handler(event, _LambdaContext(5))["body"] == "ok"
    assert [e for e in wire if e[0] == "send"] == [("send", "A"), ("send", "B")]
    assert sum(s for kind, s in wire if kind == "sleep") == 0


def test_a_long_schedule_message_text_pauses_between_its_chunks(table, wire):
    """schedule_message()'s text runs only on the scheduler Lambda."""
    handler = _app_handler("scheduler", wire)
    event = {
        "source": "socialagent_scheduler",
        "phone": PHONE,
        "message": {"type": "text", "text": LONG},
        "schedule_id": f"reminder-{DIGITS}",
        "scheduled_time": NINE,
    }
    assert _deliver(handler, event)["body"] == "ok"
    assert wire == [("send", "A"), ("sleep", 1.5), ("send", "B")]


# ── A text message sent as send_message ───────────────────────────────────────


def _text_message(body: str) -> dict:
    """A scheduled send_message of what messages.text() builds."""
    return _scheduled("send_message", type="text", payload={"text": {"body": body}})


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_long_scheduled_text_message_does_not_resend_a_delivered_chunk(
    table, wire, which
):
    """The provider sends a text-type message as send_text does, chunk by chunk
    inside one call, so trying the whole call again sent chunk A twice when
    chunk B was throttled."""

    def throttle_b_once(body: str, attempt: int) -> None:
        if body.startswith("B") and attempt == 1:
            raise _eum_error("ThrottledRequestException")

    handler = _app_handler(which, wire, fail_on=throttle_b_once)
    assert _deliver(handler, _text_message(LONG))["body"] == "ok"
    sends = [letter for kind, letter in wire if kind == "send"]
    assert sends == ["A", "B", "B"]


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_long_scheduled_text_message_pauses_between_its_chunks(table, wire, which):
    handler = _app_handler(which, wire)
    assert _deliver(handler, _text_message(LONG))["body"] == "ok"
    assert wire == [("send", "A"), ("sleep", 1.5), ("send", "B")]


# ── Retries that fit the Lambda's time ────────────────────────────────────────


def _throttled_once(provider):
    provider.send_template = AsyncMock(
        side_effect=[_eum_error("ThrottledRequestException"), "m3"]
    )
    return _scheduled("send_template", template_name="reminder_clean", params=[])


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_retry_that_would_outlast_the_lambda_is_not_made(
    table, provider, which, caplog
):
    """One more try can take the AWS SDK's own retries, whose pauses add up to
    15 s. Started with too little time left, the Lambda times out mid-try with
    the record kept: Lambda's retry answers duplicate and nothing says the send
    was lost. So the handler logs the failure and raises while it can."""
    event = _throttled_once(provider)
    handler = _handler(which, provider, table)
    sleep = AsyncMock()
    with (
        patch("socialagent.handlers.scheduled.asyncio.sleep", new=sleep),
        caplog.at_level(logging.ERROR),
        pytest.raises(ClientError),
    ):
        handler(json.loads(json.dumps(event)), _LambdaContext(15))
    assert provider.send_template.await_count == 1
    sleep.assert_not_awaited()
    assert "failed permanently" in caplog.text
    assert "15 s left" in caplog.text


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_retry_that_fits_the_lambda_is_made(table, provider, which):
    event = _throttled_once(provider)
    handler = _handler(which, provider, table)
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        got = handler(json.loads(json.dumps(event)), _LambdaContext(60))
    assert got["body"] == "ok"
    assert provider.send_template.await_count == 2


def test_each_chunk_of_a_long_text_stops_retrying_near_the_timeout(table, provider):
    """The time left is read before each pause: after chunk A took most of it,
    a throttled chunk B is not tried again."""
    left = {"s": 60.0}

    class Clock:
        def get_remaining_time_in_millis(self) -> int:
            return int(left["s"] * 1000)

    async def send_text(_phone, chunk):
        # Each throttled try spends what the AWS SDK's own retries would.
        if chunk.startswith("A") and provider.send_text.await_count < 3:
            left["s"] -= 15
            raise _eum_error("ThrottledRequestException")
        if chunk.startswith("B"):
            left["s"] -= 15
            raise _eum_error("ThrottledRequestException")
        return "m1"

    async def sleep(seconds: float) -> None:
        left["s"] -= seconds

    provider.send_text = AsyncMock(side_effect=send_text)
    handler = _handler("notification", provider, table)
    event = json.loads(json.dumps(_scheduled("send_text", text=LONG)))
    with (
        patch("socialagent.handlers.scheduled.asyncio.sleep", new=sleep),
        pytest.raises(ClientError),
    ):
        handler(event, Clock())
    # A: throttled twice, sent on its third try. B: throttled on its first,
    # with 6 s left, too few for another.
    chunks = [c.args[1][0] for c in provider.send_text.await_args_list]
    assert chunks == ["A", "A", "A", "B"]
    assert left["s"] == 6


# ── What a lost send leaves in the log ───────────────────────────────────────


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_lost_scheduled_send_names_its_firing(table, provider, which, caplog):
    """On the notification Lambda this line is all a lost send leaves: Lambda's
    retry answers duplicate, so the payload reaches no DLQ, and one error does
    not set off the error alarm. So it names the schedule and the time it was
    due, as the line for a repeated delivery does, the number masked."""
    provider.send_text = AsyncMock(side_effect=_eum_error("ValidationException"))
    handler = _handler(which, provider, table)
    with caplog.at_level(logging.ERROR), pytest.raises(ClientError):
        _deliver(handler, _scheduled("send_text", text="Hola"))
    assert _errors(caplog) == [
        "Scheduled send send_text ...0100-send_text-abcd1234 due "
        f"{NINE} failed permanently"
    ]
    assert DIGITS not in caplog.text


def test_a_lost_schedule_message_text_names_its_firing(table, provider, caplog):
    provider.send_text = AsyncMock(side_effect=_eum_error("ValidationException"))
    handler = _handler("scheduler", provider, table)
    event = {
        "source": "socialagent_scheduler",
        "phone": PHONE,
        "message": {"type": "text", "text": "Hola"},
        "schedule_id": f"reminder-{DIGITS}",
        "scheduled_time": NINE,
    }
    with caplog.at_level(logging.ERROR), pytest.raises(ClientError):
        _deliver(handler, event)
    assert _errors(caplog) == [
        f"Scheduled send direct_text reminder-...0100 due {NINE} failed permanently"
    ]


# ── A handler built without the state table ──────────────────────────────────


@pytest.mark.parametrize("which", LAMBDAS)
def test_a_handler_built_without_state_says_so(provider, monkeypatch, caplog, which):
    """make_notification_handler(provider, memory_writer), the call before the
    firing record, keeps none even on a stack whose table is set. The WARNING
    said STATE_TABLE_NAME was empty, which sent the reader to the wrong fix."""
    monkeypatch.setenv("STATE_TABLE_NAME", TABLE)
    handler = _handler(which, provider, None)
    event = _scheduled("send_text", text="Hola")
    with caplog.at_level(logging.WARNING):
        _deliver(handler, event)
        _deliver(handler, event)
    assert provider.send_text.await_count == 2
    (warning,) = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert "STATE_TABLE_NAME is empty" not in warning
    assert "state=" in warning
    assert f"{which} Lambda" in warning
