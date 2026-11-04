# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The notification Lambda sends each firing of a scheduled send at most once.

On a stack without the scheduler Lambda, a schedule's ``send_text``,
``send_template`` or ``send_message`` targets the notification Lambda. It had
no firing record and keeps Lambda's async retries (``notify`` relies on them),
so a firing that EventBridge Scheduler or Lambda's async queue delivered twice
sent twice, and a send that failed after a partial delivery was run again by
Lambda's retry. A payload with a top-level ``schedule_id`` now gets the
scheduler Lambda's record: same key, same TTL, written before the send and
kept if it fails, so the retry finds it and sends nothing, and the send tries
a transient failure again itself. A payload from ``notify`` or the console has
no ``schedule_id`` and is sent as before, without touching the state table.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from socialagent.config import SocialAgentConfig
from socialagent.handlers.event import EventContext
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduled import firing_record_key
from socialagent.state.table import SocialAgentState

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import config as rt_config  # noqa: E402
import scheduler as rt_sched  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TABLE = "dev-bot-state"
PHONE = "+525555550100"
DIGITS = "525555550100"
PLACEHOLDER = "<aws.scheduler.scheduled-time>"
NINE = "2026-10-30T09:00:00Z"
TEN = "2026-10-30T10:00:00Z"
NOTIF = "arn:aws:lambda:us-east-1:123456789012:function:dev-bot-notification"
LOGGER = "socialagent.handlers.notification"

# What each send needs in its data to go out, as the notification Lambda reads it.
_SEND_DATA = {
    "send_message": {"type": "interactive", "payload": {"k": "v"}},
    "send_template": {"template_name": "reminder_clean", "params": []},
    "send_text": {"text": "Hola"},
}
SENDS = sorted(_SEND_DATA)


def _config(table_name: str) -> SocialAgentConfig:
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
        yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def _items(table) -> list[dict]:
    return table.scan()["Items"]


@pytest.fixture
def provider():
    p = AsyncMock()
    p.send_text = AsyncMock(return_value="m1")
    p.send_message = AsyncMock(return_value="m2")
    p.send_template = AsyncMock(return_value="m3")
    return p


def _sends(provider) -> int:
    return (
        provider.send_text.await_count
        + provider.send_template.await_count
        + provider.send_message.await_count
    )


def _scheduled(op: str = "send_text", when: str | None = NINE, **data) -> dict:
    """A schedule's payload as EventBridge Scheduler delivers it here."""
    event = {
        "source": "socialagent",
        "operation": op,
        "data": {"phone": PHONE, **(data or _SEND_DATA[op])},
        "schedule_id": f"{DIGITS}-{op}-abcd1234",
    }
    if when is not None:
        event["scheduled_time"] = when
    return event


def _deliver(handler, event: dict) -> dict:
    # Each delivery is a fresh copy of the payload, as Lambda hands it over.
    return handler(json.loads(json.dumps(event)))


def _handler(provider, store):
    return make_notification_handler(provider, AsyncMock(), state=store)


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == LOGGER
    ]


# ── One firing, delivered twice ───────────────────────────────────────────────


@pytest.mark.parametrize("op", SENDS)
def test_a_repeated_delivery_of_a_scheduled_send_sends_once(table, provider, op):
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    assert _deliver(handler, _scheduled(op)) == {"statusCode": 200, "body": "ok"}
    assert _deliver(handler, _scheduled(op)) == {
        "statusCode": 200,
        "body": "duplicate",
    }
    assert _sends(provider) == 1


def test_each_firing_of_a_recurring_schedule_sends(table, provider):
    # One schedule id, two due times: the id alone would swallow the second.
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    _deliver(handler, _scheduled(when=NINE))
    _deliver(handler, _scheduled(when=TEN))
    _deliver(handler, _scheduled(when=TEN))
    assert provider.send_text.await_count == 2


def test_the_record_is_the_scheduler_lambdas(table, provider):
    """The same key and TTL the scheduler Lambda writes, outside the user's
    partition, and nothing of the number in it."""
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    before = int(time.time())
    event = _scheduled()
    _deliver(handler, event)
    (item,) = _items(table)
    pk, name = firing_record_key(event["schedule_id"], NINE)
    assert (item["pk"], item["sk"]) == (pk, f"claim#{name}")
    assert DIGITS[-4:] not in item["pk"]
    # EventBridge retries a firing for up to 24 h, and Lambda's async queue
    # keeps an event for up to 6 h.
    assert int(item["ttl"]) >= before + 30 * 3600


def test_the_skip_is_logged_without_the_number(table, provider, caplog):
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    _deliver(handler, _scheduled())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _deliver(handler, _scheduled())
    assert "repeated delivery" in caplog.text
    assert DIGITS not in caplog.text


# ── A send that fails ─────────────────────────────────────────────────────────


def test_a_failed_send_keeps_the_record_so_lambdas_retry_sends_nothing(table, provider):
    """Lambda retries a failed async event twice. A retry after a send that
    went out in part would send it again, so the record is kept."""
    provider.send_template = AsyncMock(side_effect=ValueError("bad template"))
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    with pytest.raises(ValueError, match="bad template"):
        _deliver(handler, _scheduled("send_template"))
    assert _deliver(handler, _scheduled("send_template"))["body"] == "duplicate"
    assert provider.send_template.await_count == 1
    assert len(_items(table)) == 1


@pytest.mark.parametrize("op", SENDS)
def test_a_throttled_scheduled_send_is_tried_again_in_the_handler(table, provider, op):
    """Lambda's retry no longer reaches a scheduled send, so the handler tries
    a transient failure again, as the scheduler Lambda does."""
    setattr(
        provider, op, AsyncMock(side_effect=[Exception("ThrottlingException"), "m"])
    )
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        assert _deliver(handler, _scheduled(op))["body"] == "ok"
    assert getattr(provider, op).await_count == 2


def test_a_long_scheduled_text_does_not_resend_a_delivered_chunk(table, provider):
    wire: list[str] = []

    async def send_text(_phone, chunk):
        wire.append(chunk)
        if chunk.startswith("B") and wire.count(chunk) == 1:
            raise Exception("ThrottlingException")
        return "m"

    provider.send_text = AsyncMock(side_effect=send_text)
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    text = "A" * 1600 + "B" * 200
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        _deliver(handler, _scheduled("send_text", text=text))
    assert sum(c.startswith("A") for c in wire) == 1
    assert sum(c.startswith("B") for c in wire) == 2


def _unrecorded(case: str, table_store):
    """A store, and a payload, for a firing that runs with no record."""
    if case == "no_table":
        return None, _scheduled("send_template")
    if case == "no_scheduled_time":
        return table_store, _scheduled("send_template", when=None)
    broken = MagicMock(spec=SocialAgentState)
    broken.table_configured = True
    broken.claim_once = AsyncMock(side_effect=Exception("ProvisionedThroughput"))
    return broken, _scheduled("send_template")


@pytest.mark.parametrize("case", ["no_table", "no_scheduled_time", "dynamodb_error"])
def test_an_unrecorded_scheduled_send_keeps_lambdas_retries_instead(
    table, provider, case
):
    """With no record, Lambda's two async retries run the send again, so the
    handler adds none: a send that keeps failing is tried 3 times, not 9, and
    a 500 that WhatsApp took is not sent 9 times over."""
    provider.send_template = AsyncMock(side_effect=Exception("ThrottlingException"))
    store, event = _unrecorded(case, SocialAgentState(_config(TABLE)))
    handler = _handler(provider, store)
    sleep = AsyncMock()
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=sleep):
        # The first invocation and Lambda's two retries.
        for _ in range(3):
            with pytest.raises(Exception, match="ThrottlingException"):
                _deliver(handler, event)
    assert provider.send_template.await_count == 3
    sleep.assert_not_awaited()
    assert _items(table) == []


def test_a_transient_dynamodb_error_leaves_lambdas_retries_to_the_record(
    table, provider
):
    """Lambda's retries keep a send only while the table keeps failing. Each
    retry claims again: once the table answers, that retry records the firing
    and tries the send in the handler, so the next retry answers duplicate and
    the event reaches no DLQ, as for a send recorded at once."""
    store = SocialAgentState(_config(TABLE))
    real = store.claim_once
    outage = [
        ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "try again"}},
            "PutItem",
        )
    ]

    async def claim_once(*args, **kwargs):
        if outage:
            raise outage.pop()
        return await real(*args, **kwargs)

    store.claim_once = claim_once
    provider.send_text = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "ValidationException", "Message": "refused"}},
            "SendWhatsAppMessage",
        )
    )
    handler = _handler(provider, store)
    outcomes = []
    # The first invocation and Lambda's two retries.
    for _ in range(3):
        try:
            outcomes.append(_deliver(handler, _scheduled())["body"])
        except ClientError as exc:
            outcomes.append(exc.response["Error"]["Code"])
    assert outcomes == ["ValidationException", "ValidationException", "duplicate"]
    assert provider.send_text.await_count == 2
    assert len(_items(table)) == 1


def test_a_send_from_notify_is_not_tried_again_in_the_handler(provider):
    """Lambda's async retries still cover it, so the handler adds none."""
    provider.send_text = AsyncMock(side_effect=Exception("ThrottlingException"))
    handler = _handler(provider, MagicMock(spec=SocialAgentState))
    with pytest.raises(Exception, match="ThrottlingException"):
        _deliver(
            handler, {"operation": "send_text", "data": {"phone": PHONE, "text": "x"}}
        )
    assert provider.send_text.await_count == 1


# ── Sends that are not a schedule's ───────────────────────────────────────────


_NOT_SCHEDULED = [
    # notify outside a turn, with log_to_memory, and an immediate mid-turn send.
    {
        "operation": "send_text",
        "data": {"phone": PHONE, "text": "x", "memory_text": "x"},
    },
    {
        "operation": "send_text",
        "data": {"phone": PHONE, "text": "x", "memory_text": ""},
    },
    {
        "operation": "send_template",
        "data": {
            "phone": PHONE,
            "template_name": "t",
            "params": [],
            "log_to_memory": True,
        },
    },
    {
        "operation": "send_message",
        "data": {
            "phone": PHONE,
            "type": "interactive",
            "payload": {},
            "memory_text": "",
        },
    },
]


@pytest.mark.parametrize("event", _NOT_SCHEDULED, ids=lambda e: e["operation"])
def test_a_send_from_notify_touches_no_state(provider, event):
    store = MagicMock(spec=SocialAgentState)
    handler = _handler(provider, store)
    _deliver(handler, dict(event, source="socialagent"))
    _deliver(handler, dict(event, source="socialagent"))
    # Not deduplicated: notify relies on Lambda's retries, and every call is
    # its own send.
    assert _sends(provider) == 2
    assert store.mock_calls == []


def test_a_send_from_notify_writes_no_record(table, provider):
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    for event in _NOT_SCHEDULED:
        _deliver(handler, dict(event, source="socialagent"))
    assert _items(table) == []


def test_typing_and_uploads_are_untouched(table, provider):
    provider.upload_media_from_s3 = AsyncMock(return_value="mid")
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    _deliver(
        handler,
        {"operation": "send_typing", "data": {"phone": PHONE, "message_id": "w"}},
    )
    got = _deliver(
        handler,
        {"operation": "upload_media", "data": {"bucket": "b", "key": "voice/k"}},
    )
    assert json.loads(got["body"]) == {"media_id": "mid"}
    assert _items(table) == []


# ── Where no record can be written ────────────────────────────────────────────


@pytest.mark.parametrize("state", ["empty", "none"])
def test_without_the_state_table_it_sends_and_warns_once(provider, caplog, state):
    store = SocialAgentState(_config("")) if state == "empty" else None
    with patch("socialagent.state.table.boto3.resource") as resource:
        handler = _handler(provider, store)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                _deliver(handler, _scheduled())
    assert provider.send_text.await_count == 3
    resource.assert_not_called()
    (warning,) = _warnings(caplog)
    assert "notification Lambda" in warning
    assert "state table" in warning


@pytest.mark.parametrize("when", [None, PLACEHOLDER])
def test_a_schedule_with_no_scheduled_time_sends_and_warns_once(
    table, provider, caplog, when
):
    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    with caplog.at_level(logging.WARNING):
        _deliver(handler, _scheduled(when=when))
        _deliver(handler, _scheduled(when=when))
    assert provider.send_text.await_count == 2
    assert _items(table) == []
    (warning,) = _warnings(caplog)
    assert "scheduled_time" in warning
    assert DIGITS not in warning


def test_a_failed_record_write_sends_and_masks_the_number(table, provider, caplog):
    # Fail open: a reminder lost to a DynamoDB error is worse than a rare
    # duplicate. The error text can quote the key, so it is masked.
    store = SocialAgentState(_config(TABLE))
    broken = MagicMock()
    broken.put_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"denied for {PHONE}"}},
        "PutItem",
    )
    handler = _handler(provider, store)
    with (
        patch.object(SocialAgentState, "_table", return_value=broken),
        caplog.at_level(logging.WARNING),
    ):
        assert _deliver(handler, _scheduled())["body"] == "ok"
    assert provider.send_text.await_count == 1
    assert _warnings(caplog)
    assert DIGITS not in caplog.text


# ── The schedules that target this Lambda, end to end ────────────────────────


def _fired(payload: dict, when: str) -> dict:
    """The Input as EventBridge delivers it: the placeholder filled in."""
    assert payload["scheduled_time"] == PLACEHOLDER
    return dict(payload, scheduled_time=when)


@pytest.mark.parametrize("op", SENDS)
def test_a_handler_schedule_without_the_scheduler_lambda_sends_once(
    table, provider, monkeypatch, op
):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "m")
    monkeypatch.setenv("NOTIFICATION_SERVICE_ARN", NOTIF)
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/s")
    monkeypatch.setenv("SCHEDULER_PREFIX", "dev-bot")
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "")
    ctx = EventContext(
        AsyncMock(), AsyncMock(), None, config=SocialAgentConfig.from_env()
    )
    client = MagicMock()
    with patch("socialagent.scheduler.eventbridge.boto3.client", return_value=client):
        asyncio.run(ctx.schedule(op, PHONE, delay_minutes=5, data=_SEND_DATA[op]))
    target = client.create_schedule.call_args.kwargs["Target"]
    assert target["Arn"] == NOTIF

    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    fired = _fired(json.loads(target["Input"]), NINE)
    _deliver(handler, fired)
    _deliver(handler, fired)
    assert getattr(provider, op).await_count == 1


@pytest.mark.parametrize("op", SENDS)
def test_an_agent_schedule_without_the_scheduler_lambda_sends_once(
    table, provider, monkeypatch, op
):
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", NOTIF)
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "")
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/s")
    monkeypatch.setenv("SCHEDULER_PREFIX", "dev-bot")
    monkeypatch.setenv("MEDIA_BUCKET", "media")
    monkeypatch.setenv("STATE_TABLE_NAME", TABLE)
    monkeypatch.setattr(rt_sched, "config", rt_config.AgentConfig.from_env())
    monkeypatch.setattr(rt_sched, "_count_user_schedules", lambda phone: 0)
    monkeypatch.setattr(rt_sched, "_schedule_exists", MagicMock(return_value=False))
    monkeypatch.setattr(rt_sched, "_scheduler_client", MagicMock())
    created = MagicMock()
    monkeypatch.setattr(rt_sched, "_create_schedule", created)
    asyncio.run(rt_sched.schedule(op, PHONE, 5, _SEND_DATA[op]))
    assert created.call_args.args[2] == NOTIF

    handler = _handler(provider, SocialAgentState(_config(TABLE)))
    fired = _fired(created.call_args.args[4], TEN)
    _deliver(handler, fired)
    _deliver(handler, fired)
    assert getattr(provider, op).await_count == 1


def test_the_blueprint_notification_handler_gets_the_state_table(table, monkeypatch):
    """app.notification_handler(), as the Lambda builds it, records firings."""
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/a"
    )
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("STATE_TABLE_NAME", TABLE)
    path = REPO / "blueprints" / "minimal" / "handlers" / "notification.py"
    spec = importlib.util.spec_from_file_location("minimal_notification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    send = AsyncMock(return_value="m")
    monkeypatch.setattr(module.app.provider, "send_text", send)
    module.handler(json.loads(json.dumps(_scheduled())))
    module.handler(json.loads(json.dumps(_scheduled())))
    assert send.await_count == 1
