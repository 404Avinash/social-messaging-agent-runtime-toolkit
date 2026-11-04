# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The scheduler Lambda runs each firing of a schedule at most once.

EventBridge Scheduler delivers at least once, and Lambda's async queue can hand
the same event to the function twice even when the first run succeeded, so
``maximum_retry_attempts = 0`` alone does not stop a reminder or a scheduled
agent turn from running twice. Before it runs a built-in send or an
``@app.on_schedule`` handler, the scheduler Lambda writes a record for the
firing to the state table with a conditional put: the first delivery writes
it and runs, a repeat finds it and runs nothing.

The record is keyed by the schedule id and EventBridge's
``<aws.scheduler.scheduled-time>``, which every schedule's Input now asks for.
The id alone would block every later firing of a recurring schedule. A
schedule created before the change has no scheduled time and a stack without
the state table has nowhere to write: both still run, and say once that a
repeated delivery would run again.
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

from socialagent import constants as layer_constants
from socialagent.config import SocialAgentConfig
from socialagent.handlers.scheduler_event import make_scheduler_handler
from socialagent.scheduler import eventbridge
from socialagent.state.table import SocialAgentState

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import constants as rt_constants  # noqa: E402
import scheduler as rt_sched  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TABLE = "dev-bot-state"
PHONE = "+525555550100"
DIGITS = "525555550100"
PLACEHOLDER = "<aws.scheduler.scheduled-time>"
NINE = "2026-10-30T09:00:00Z"
TEN = "2026-10-30T10:00:00Z"


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


def _writer():
    w = AsyncMock()
    w.log_assistant_message = AsyncMock()
    return w


def _operation(op: str = "checkin", when: str | None = NINE, **data) -> dict:
    event = {
        "source": "socialagent",
        "operation": op,
        "data": {"phone": PHONE, **data},
        "schedule_id": f"{DIGITS}-{op}-abcd1234",
    }
    if when is not None:
        event["scheduled_time"] = when
    return event


def _deliver(handler, event: dict) -> dict:
    # Each delivery is a fresh copy of the payload, as Lambda hands it over.
    return handler(json.loads(json.dumps(event)))


# ── One firing, delivered twice ───────────────────────────────────────────────


def test_a_repeated_delivery_runs_an_on_schedule_handler_once(table, provider):
    fn = AsyncMock()
    handler = make_scheduler_handler(
        provider, _writer(), {"checkin": fn}, state=SocialAgentState(_config(TABLE))
    )
    assert _deliver(handler, _operation()) == {"statusCode": 200, "body": "ok"}
    assert _deliver(handler, _operation()) == {"statusCode": 200, "body": "duplicate"}
    assert fn.await_count == 1


_BUILT_INS = {
    "send_text": _operation("send_text", text="Hola"),
    "send_template": _operation("send_template", template_name="reminder_clean"),
    "send_message": _operation("send_message", type="interactive", payload={}),
    "direct_text": {
        "source": "socialagent_scheduler",
        "phone": PHONE,
        "message": {"type": "text", "text": "Hola"},
        "schedule_id": "direct-1",
        "scheduled_time": NINE,
    },
    "direct_rich": {
        "source": "socialagent_scheduler",
        "phone": PHONE,
        "message": {"type": "interactive", "payload": {"k": "v"}, "text": "Pick"},
        "schedule_id": "direct-2",
        "scheduled_time": NINE,
    },
}


@pytest.mark.parametrize("kind", sorted(_BUILT_INS))
def test_a_repeated_delivery_sends_a_built_in_once(table, provider, kind):
    handler = make_scheduler_handler(
        provider, _writer(), {}, state=SocialAgentState(_config(TABLE))
    )
    _deliver(handler, _BUILT_INS[kind])
    _deliver(handler, _BUILT_INS[kind])
    sends = (
        provider.send_text.await_count
        + provider.send_template.await_count
        + provider.send_message.await_count
    )
    assert sends == 1


def test_each_firing_of_a_recurring_schedule_runs(table, provider):
    # One schedule id, two due times: the id alone would swallow the second.
    fn = AsyncMock()
    handler = make_scheduler_handler(
        provider, _writer(), {"checkin": fn}, state=SocialAgentState(_config(TABLE))
    )
    _deliver(handler, _operation(when=NINE))
    _deliver(handler, _operation(when=TEN))
    _deliver(handler, _operation(when=TEN))
    assert fn.await_count == 2


def test_a_handler_that_fails_keeps_the_record(table, provider):
    # At most one run per firing: a redelivery after a partial send must not
    # act as a retry, which Lambda's retries are turned off to prevent.
    fn = AsyncMock(side_effect=RuntimeError("send failed"))
    handler = make_scheduler_handler(
        provider, _writer(), {"checkin": fn}, state=SocialAgentState(_config(TABLE))
    )
    with pytest.raises(RuntimeError):
        _deliver(handler, _operation())
    assert _deliver(handler, _operation())["body"] == "duplicate"
    assert fn.await_count == 1
    assert len(_items(table)) == 1


def test_an_unknown_operation_writes_no_record(table, provider):
    handler = make_scheduler_handler(
        provider, _writer(), {}, state=SocialAgentState(_config(TABLE))
    )
    assert _deliver(handler, _operation("nope"))["statusCode"] == 400
    assert _items(table) == []


def test_the_record_names_the_firing_and_not_the_user(table, provider):
    handler = make_scheduler_handler(
        provider,
        _writer(),
        {"checkin": AsyncMock()},
        state=SocialAgentState(_config(TABLE)),
    )
    before = int(time.time())
    _deliver(handler, _operation())
    (item,) = _items(table)
    # Outside the user's partition, so the agent's list_keys(phone) never shows
    # it, and the schedule id, which holds the number, is stored only hashed.
    assert item["pk"].startswith("_schedule#")
    assert DIGITS[-4:] not in item["pk"]
    assert item["sk"] == f"claim#firing#{NINE}"
    # EventBridge retries a firing for up to 24 h, and Lambda's async queue
    # keeps an event for up to 6 h.
    assert int(item["ttl"]) >= before + 30 * 3600


def test_two_schedules_due_at_the_same_time_both_run(table, provider):
    fn = AsyncMock()
    handler = make_scheduler_handler(
        provider, _writer(), {"checkin": fn}, state=SocialAgentState(_config(TABLE))
    )
    first = _operation()
    second = dict(_operation(), schedule_id=f"{DIGITS}-checkin-ffff0000")
    _deliver(handler, first)
    _deliver(handler, second)
    assert fn.await_count == 2


def test_the_skip_is_logged_without_the_number(table, provider, caplog):
    handler = make_scheduler_handler(
        provider,
        _writer(),
        {"checkin": AsyncMock()},
        state=SocialAgentState(_config(TABLE)),
    )
    _deliver(handler, _operation())
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.scheduler_event"):
        _deliver(handler, _operation())
    assert "repeated delivery" in caplog.text
    assert DIGITS not in caplog.text


# ── Where no record can be written ────────────────────────────────────────────


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "socialagent.handlers.scheduler_event"
    ]


@pytest.mark.parametrize("state", ["empty", "none"])
def test_without_the_state_table_it_runs_and_warns_once(provider, caplog, state):
    fn = AsyncMock()
    store = SocialAgentState(_config("")) if state == "empty" else None
    with patch("socialagent.state.table.boto3.resource") as resource:
        handler = make_scheduler_handler(
            provider, _writer(), {"checkin": fn}, state=store
        )
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                _deliver(handler, _operation())
    assert fn.await_count == 3
    resource.assert_not_called()
    (warning,) = _warnings(caplog)
    assert "state table" in warning


def test_dev_mode_runs_without_a_warning(provider, caplog):
    # socialagent dev imports handlers/scheduler.py, building this handler with
    # the dev sentinel as the table name; it never redelivers anything.
    fn = AsyncMock()
    store = SocialAgentState(_config(layer_constants.DEV_MODE_VALUE))
    handler = make_scheduler_handler(provider, _writer(), {"checkin": fn}, state=store)
    with caplog.at_level(logging.WARNING):
        _deliver(handler, _operation())
    assert fn.await_count == 1
    assert _warnings(caplog) == []


@pytest.mark.parametrize("when", [None, PLACEHOLDER])
def test_a_schedule_with_no_scheduled_time_runs_and_warns_once(
    table, provider, caplog, when
):
    # None: created before its Input asked for the time. The placeholder: the
    # stored Input invoked by hand, which EventBridge never filled in.
    fn = AsyncMock()
    handler = make_scheduler_handler(
        provider, _writer(), {"checkin": fn}, state=SocialAgentState(_config(TABLE))
    )
    with caplog.at_level(logging.WARNING):
        _deliver(handler, _operation(when=when))
        _deliver(handler, _operation(when=when))
    assert fn.await_count == 2
    assert _items(table) == []
    (warning,) = _warnings(caplog)
    assert "scheduled_time" in warning
    assert DIGITS not in warning


def test_a_failed_record_write_runs_the_firing_and_masks_the_number(
    table, provider, caplog
):
    # Fail open: a reminder lost to a DynamoDB error is worse than a rare
    # duplicate. The error text can quote the key, so it is masked.
    fn = AsyncMock()
    store = SocialAgentState(_config(TABLE))
    broken = MagicMock()
    broken.put_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": f"failed for {PHONE}"}},
        "PutItem",
    )
    handler = make_scheduler_handler(provider, _writer(), {"checkin": fn}, state=store)
    with (
        patch.object(SocialAgentState, "_table", return_value=broken),
        caplog.at_level(logging.WARNING),
    ):
        _deliver(handler, _operation())
    assert fn.await_count == 1
    assert _warnings(caplog)
    assert DIGITS not in caplog.text


# ── Every schedule asks EventBridge for its due time ─────────────────────────


def _sdk_inputs() -> list[dict]:
    client = MagicMock()
    client.exceptions.ConflictException = type("Conflict", (Exception,), {})
    target, role = (
        "arn:aws:lambda:us-east-1:1:function:x-scheduler",
        "arn:aws:iam::1:role/r",
    )
    with patch.object(eventbridge.boto3, "client", return_value=client):
        asyncio.run(
            eventbridge.schedule_operation(
                "checkin", PHONE, {}, target, role, cron="0 9 * * ? *"
            )
        )
        asyncio.run(
            eventbridge.schedule_message(
                "sid", PHONE, "hola", target, role, delay_minutes=5
            )
        )
    return [
        json.loads(c.kwargs["Target"]["Input"])
        for c in client.create_schedule.call_args_list
    ]


def test_the_sdk_schedules_ask_for_the_scheduled_time():
    inputs = _sdk_inputs()
    assert len(inputs) == 2
    assert all(i["scheduled_time"] == PLACEHOLDER for i in inputs)


class _DeployedConfig:
    is_dev_mode = False
    scheduler_prefix = "dev-bot"
    scheduler_lambda_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:dev-bot-scheduler"
    )
    has_scheduler_lambda = True
    scheduler_role_arn = "arn:aws:iam::123456789012:role/scheduler"
    region = "us-east-1"


def test_the_agent_schedule_asks_for_the_scheduled_time(monkeypatch):
    monkeypatch.setattr(rt_sched, "config", _DeployedConfig())
    monkeypatch.setattr(rt_sched, "_count_user_schedules", lambda phone: 0)
    created = MagicMock()
    monkeypatch.setattr(rt_sched, "_create_schedule", created)
    asyncio.run(rt_sched.schedule("reminder", PHONE, delay_minutes=30, data={}))
    assert created.call_args.args[4]["scheduled_time"] == PLACEHOLDER


def test_both_layers_name_the_same_attribute():
    # The runtime package cannot import the Lambda layer, so each keeps a copy.
    assert rt_constants.SCHEDULED_TIME_ATTRIBUTE == PLACEHOLDER
    assert layer_constants.SCHEDULED_TIME_ATTRIBUTE == PLACEHOLDER


# ── A project's own handlers, through the deployed wiring ────────────────────


def _load_personal_assistant(monkeypatch):
    """personal-assistant's handlers/scheduler.py, loaded as its Lambda loads it."""
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/a"
    )
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("STATE_TABLE_NAME", TABLE)
    path = REPO / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("pa_scheduler_firing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for send in ("send_text", "send_template", "send_message"):
        monkeypatch.setattr(module.app.provider, send, AsyncMock(return_value="m"))
    monkeypatch.setattr(module.app.memory_writer, "log_assistant_message", AsyncMock())
    return module


def test_a_scheduled_agent_task_delivered_twice_runs_one_turn(table, monkeypatch):
    module = _load_personal_assistant(monkeypatch)
    turn = MagicMock(return_value={"result": "done"})
    monkeypatch.setattr(module, "invoke_runtime", turn)
    task = _operation("invoke_agent", prompt="summarize my pending items")
    # The module's own handler: app.scheduler_handler() wires in the state table.
    module.handler(json.loads(json.dumps(task)))
    module.handler(json.loads(json.dumps(task)))
    assert turn.call_count == 1
    assert module.app.provider.send_template.await_count == 1


def test_a_reminder_delivered_twice_sends_once(table, monkeypatch):
    module = _load_personal_assistant(monkeypatch)
    reminder = _operation("reminder", message="Tomar pastilla")
    module.handler(json.loads(json.dumps(reminder)))
    module.handler(json.loads(json.dumps(reminder)))
    assert module.app.provider.send_template.await_count == 1
