# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduling on a stack without the scheduler Lambda.

There Terraform sets SCHEDULER_LAMBDA_ARN to "" and a schedule targets the
notification Lambda, which runs only its built-in sends and answers 400 to
anything else. EventBridge counts the 400 as delivered, so a one-shot for an
@app.on_schedule operation deleted itself unsent, and a recurring one failed
on every firing while it held one of the user's schedules. Both layers now
refuse such a schedule when it is asked for, and still schedule the sends the
notification Lambda runs. ``socialagent dev`` refuses what the YAML's stack
would refuse, and nothing more.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent import constants as layer_constants
from socialagent.cli import dev
from socialagent.config import SocialAgentConfig
from socialagent.handlers import notification
from socialagent.handlers.event import EventContext, SchedulerUnavailableError
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduler_event import make_scheduler_handler

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import config as rt_config  # noqa: E402
import constants as rt_constants  # noqa: E402
import scheduler as rt_sched  # noqa: E402

REPO = Path(__file__).parent.parent
DEV = "__socialagent_dev__"
NOTIF = "arn:aws:lambda:us-east-1:123456789012:function:dev-bot-notification"
SCHED = "arn:aws:lambda:us-east-1:123456789012:function:dev-bot-scheduler"
PHONE = "+525555550100"

SENDS = sorted(layer_constants.NOTIFICATION_SCHEDULE_OPERATIONS)
# What each send needs in its data to go out. The message is not a text one:
# that goes out through the provider's send_text, a chunk at a time
# (tests/test_scheduled_sends.py).
_SEND_DATA = {
    "send_message": {"type": "interactive", "payload": {"interactive": {}}},
    "send_template": {"template_name": "order_update", "params": ["A1"]},
    "send_text": {"text": "hola"},
}
# Routed by the notification Lambda but not sends a schedule can make: a typing
# indicator needs the id of a message seconds old, an upload's media id would
# reach nobody, and a health check is the deploy's own probe.
_ROUTED_NOT_SCHEDULABLE = {"health_check", "send_typing", "upload_media"}


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


# ── Lambda layer: EventContext.schedule ───────────────────────────────────────


def _handler_ctx(monkeypatch, scheduler_env: str | None) -> EventContext:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "m")
    monkeypatch.setenv("NOTIFICATION_SERVICE_ARN", NOTIF)
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/s")
    monkeypatch.setenv("SCHEDULER_PREFIX", "dev-bot")
    _set(monkeypatch, "SCHEDULER_LAMBDA_ARN", scheduler_env)
    return EventContext(
        AsyncMock(), AsyncMock(), None, config=SocialAgentConfig.from_env()
    )


@pytest.mark.parametrize("scheduler_env", ["", None])
@pytest.mark.parametrize("operation", ["order_followup", "reminder"])
def test_a_handler_operation_is_refused_and_nothing_is_created(
    monkeypatch, scheduler_env, operation
) -> None:
    ctx = _handler_ctx(monkeypatch, scheduler_env)
    with patch("socialagent.scheduler.eventbridge.boto3.client") as client:
        with pytest.raises(SchedulerUnavailableError) as raised:
            asyncio.run(ctx.schedule(operation, PHONE, delay_minutes=5, data={}))
    client.assert_not_called()
    # A ValueError, so a handler that already catches a bad argument catches
    # this too, and the message names the setting to turn on.
    assert isinstance(raised.value, ValueError)
    assert "features.scheduler_lambda: true" in str(raised.value)
    assert repr(operation) in str(raised.value)


@pytest.mark.parametrize("operation", SENDS)
def test_each_send_is_still_scheduled_and_the_notification_lambda_delivers_it(
    monkeypatch, operation
) -> None:
    """The whole path: created against the notification Lambda, and the payload
    EventBridge would deliver is one that Lambda sends (200), not drops."""
    ctx = _handler_ctx(monkeypatch, "")
    client = MagicMock()
    with patch("socialagent.scheduler.eventbridge.boto3.client", return_value=client):
        asyncio.run(
            ctx.schedule(operation, PHONE, delay_minutes=5, data=_SEND_DATA[operation])
        )
    target = client.create_schedule.call_args.kwargs["Target"]
    assert target["Arn"] == NOTIF

    provider = AsyncMock()
    handler = make_notification_handler(provider, AsyncMock())
    assert handler(json.loads(target["Input"])) == {"statusCode": 200, "body": "ok"}
    getattr(provider, operation).assert_awaited_once()


@pytest.mark.parametrize("operation", ["order_followup", "send_text"])
def test_with_the_scheduler_lambda_every_operation_targets_it(
    monkeypatch, operation
) -> None:
    ctx = _handler_ctx(monkeypatch, SCHED)
    client = MagicMock()
    with patch("socialagent.scheduler.eventbridge.boto3.client", return_value=client):
        asyncio.run(ctx.schedule(operation, PHONE, delay_minutes=5, data={"text": "x"}))
    assert client.create_schedule.call_args.kwargs["Target"]["Arn"] == SCHED


@pytest.mark.parametrize("operation", SENDS)
def test_each_send_also_runs_on_the_scheduler_lambda(monkeypatch, operation) -> None:
    """Turning the scheduler Lambda on moves these schedules to it, so it must
    run each one: it answered send_message with a 400 that EventBridge counts
    as delivered."""
    ctx = _handler_ctx(monkeypatch, SCHED)
    client = MagicMock()
    with patch("socialagent.scheduler.eventbridge.boto3.client", return_value=client):
        asyncio.run(
            ctx.schedule(operation, PHONE, delay_minutes=5, data=_SEND_DATA[operation])
        )
    target = client.create_schedule.call_args.kwargs["Target"]
    assert target["Arn"] == SCHED

    provider = AsyncMock()
    handler = make_scheduler_handler(provider, AsyncMock(), {})
    assert handler(json.loads(target["Input"])) == {"statusCode": 200, "body": "ok"}
    getattr(provider, operation).assert_awaited_once()


# ── The two copies of the operation list ──────────────────────────────────────


def test_both_layers_list_the_same_operations() -> None:
    # The runtime package cannot import the Lambda layer, so each keeps a copy.
    assert (
        rt_constants.NOTIFICATION_SCHEDULE_OPERATIONS
        == layer_constants.NOTIFICATION_SCHEDULE_OPERATIONS
    )


def test_the_list_is_every_send_the_notification_lambda_routes() -> None:
    # The delivery test above proves each listed name is sent; this catches a
    # send added to the router and not to the list, which would be refused.
    source = inspect.getsource(notification.make_notification_handler)
    routed = set(re.findall(r'operation == "(\w+)"', source))
    assert routed >= _ROUTED_NOT_SCHEDULABLE
    assert routed - _ROUTED_NOT_SCHEDULABLE == set(SENDS)


# ── Agent runtime: socialagent_runtime.scheduler.schedule ─────────────────────


def _runtime_config(monkeypatch, scheduler_env: str | None, *, dev_mode: bool):
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", DEV if dev_mode else NOTIF)
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/s")
    monkeypatch.setenv("SCHEDULER_PREFIX", "dev-bot")
    monkeypatch.setenv("MEDIA_BUCKET", "media")
    monkeypatch.setenv("STATE_TABLE_NAME", "state")
    _set(monkeypatch, "SCHEDULER_LAMBDA_ARN", scheduler_env)
    cfg = rt_config.AgentConfig.from_env()
    monkeypatch.setattr(rt_sched, "config", cfg)
    return cfg


@pytest.mark.parametrize(
    ("dev_mode", "scheduler_env", "expected"),
    [
        (False, SCHED, True),
        # Terraform sets "" without the Lambda, and always sets the variable,
        # so a deployed agent without it has no scheduler Lambda either.
        (False, "", False),
        (False, None, False),
        # `socialagent dev` sets it from the YAML: "" when the YAML turns the
        # Lambda off. A dev-mode harness that leaves it out refuses nothing.
        (True, DEV, True),
        (True, "", False),
        (True, None, True),
    ],
)
def test_has_scheduler_lambda_follows_the_variable(
    monkeypatch, dev_mode, scheduler_env, expected
) -> None:
    cfg = _runtime_config(monkeypatch, scheduler_env, dev_mode=dev_mode)
    assert cfg.has_scheduler_lambda is expected


@pytest.fixture
def aws(monkeypatch):
    """The runtime scheduler's AWS calls, recorded instead of made."""
    calls = MagicMock()
    calls.count.return_value = 0
    monkeypatch.setattr(rt_sched, "_count_user_schedules", calls.count)
    monkeypatch.setattr(rt_sched, "_schedule_exists", calls.exists)
    monkeypatch.setattr(rt_sched, "_create_schedule", calls.create)
    monkeypatch.setattr(rt_sched, "_scheduler_client", calls.client)
    return calls


@pytest.mark.parametrize("scheduler_env", ["", None])
@pytest.mark.parametrize(
    ("operation", "cron"),
    [("reminder", None), ("invoke_agent", "cron(0 14 * * ? *)")],
)
def test_the_agent_cannot_schedule_a_handler_operation(
    monkeypatch, aws, scheduler_env, operation, cron
) -> None:
    _runtime_config(monkeypatch, scheduler_env, dev_mode=False)
    with pytest.raises(rt_sched.SchedulerUnavailableError) as raised:
        asyncio.run(rt_sched.schedule(operation, PHONE, 5, {"message": "x"}, cron=cron))
    assert isinstance(raised.value, ValueError)
    assert "features.scheduler_lambda: true" in str(raised.value)
    # Refused before the cap is counted, so it took no slot and made no call.
    assert aws.mock_calls == []


@pytest.mark.parametrize("operation", SENDS)
def test_the_agent_still_schedules_the_sends(monkeypatch, aws, operation) -> None:
    _runtime_config(monkeypatch, "", dev_mode=False)
    asyncio.run(rt_sched.schedule(operation, PHONE, 5, _SEND_DATA[operation]))
    aws.create.assert_called_once()
    assert aws.create.call_args.args[2] == NOTIF


@pytest.mark.parametrize("operation", SENDS)
def test_each_send_the_agent_schedules_runs_on_the_scheduler_lambda(
    monkeypatch, aws, operation
) -> None:
    _runtime_config(monkeypatch, SCHED, dev_mode=False)
    asyncio.run(rt_sched.schedule(operation, PHONE, 5, _SEND_DATA[operation]))
    assert aws.create.call_args.args[2] == SCHED

    provider = AsyncMock()
    handler = make_scheduler_handler(provider, AsyncMock(), {})
    payload = json.loads(json.dumps(aws.create.call_args.args[4]))
    assert handler(payload) == {"statusCode": 200, "body": "ok"}
    getattr(provider, operation).assert_awaited_once()


def test_dev_mode_refuses_what_the_yamls_stack_refuses(monkeypatch, aws) -> None:
    ran = AsyncMock()
    monkeypatch.setattr(rt_sched, "_dev_schedule_handlers", {"reminder": ran})
    _runtime_config(monkeypatch, "", dev_mode=True)
    with pytest.raises(rt_sched.SchedulerUnavailableError):
        asyncio.run(rt_sched.schedule("reminder", PHONE, 5, {"message": "x"}))
    ran.assert_not_awaited()

    _runtime_config(monkeypatch, DEV, dev_mode=True)
    asyncio.run(rt_sched.schedule("reminder", PHONE, 5, {"message": "x"}))
    ran.assert_awaited_once()
    assert aws.mock_calls == []


# ── socialagent dev ───────────────────────────────────────────────────────────


def _yaml(project: Path, line: str | None) -> None:
    """A socialagent.dev.yaml with ``line`` as its features block."""
    text = (REPO / "blueprints" / "minimal" / "socialagent.dev.yaml").read_text()
    text = text.replace("__project_name__", "bot")
    block = re.search(r"^features:\n(?:  .*\n)+", text, re.M)
    assert block, "the minimal blueprint's YAML has no features block"
    text = text.replace(block.group(0), f"features:\n  {line}\n" if line else "")
    (project / "socialagent.dev.yaml").write_text(text)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("scheduler_lambda: true", True),
        ("scheduler_lambda: false", False),
        # Left out, the deployed stack has no scheduler Lambda.
        ("state_table: true", False),
        (None, False),
    ],
)
def test_dev_reads_the_flag_from_the_environments_yaml(
    tmp_path, line, expected
) -> None:
    _yaml(tmp_path, line)
    assert dev._scheduler_lambda_in_yaml(tmp_path, "dev") is expected
    # Another environment's file says nothing about this one.
    assert dev._scheduler_lambda_in_yaml(tmp_path, "prod") is None


def test_dev_does_not_stop_on_a_yaml_it_cannot_read(tmp_path, capsys) -> None:
    (tmp_path / "socialagent.dev.yaml").write_text("features: [unclosed\n")
    assert dev._scheduler_lambda_in_yaml(tmp_path, "dev") is None
    out = " ".join(capsys.readouterr().out.split())  # Rich wraps at 80 columns
    assert "not checked against features.scheduler_lambda" in out


def _dev_ctx() -> EventContext:
    return dev._dev_event_context(AsyncMock(), AsyncMock(), None, None)


def test_a_dev_handler_schedule_is_refused_when_the_yaml_turns_the_lambda_off(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "")
    with patch("socialagent.scheduler.eventbridge.boto3.client") as client:
        with pytest.raises(SchedulerUnavailableError):
            asyncio.run(_dev_ctx().schedule("order_followup", PHONE, 30, {}))
        assert asyncio.run(_dev_ctx().schedule("send_text", PHONE, 30, {"text": "x"}))
    client.assert_not_called()


@pytest.mark.parametrize("scheduler_env", [DEV, None])
def test_a_dev_handler_schedule_returns_the_deployed_id_and_creates_nothing(
    monkeypatch, capsys, scheduler_env
) -> None:
    """Dev must not refuse what the deployed stack accepts, nor reach
    EventBridge. Under the config-less EventContext dev used, schedule() failed
    on botocore's parameter check (an empty Target.Arn), and cancel_schedule()
    sent a real DeleteSchedule with the developer's credentials."""
    _set(monkeypatch, "SCHEDULER_LAMBDA_ARN", scheduler_env)
    calls = [
        ("order_followup", PHONE, 30, {"order": "A1"}),
        ("order_followup", PHONE, 30, {"order": "A1"}, "order-525555550100"),
    ]
    with patch("socialagent.scheduler.eventbridge.boto3.client") as client:
        got = [asyncio.run(_dev_ctx().schedule(*call)) for call in calls]
        asyncio.run(_dev_ctx().cancel_schedule(got[0]))
    client.assert_not_called()

    deployed = _handler_ctx(monkeypatch, SCHED)
    with patch("socialagent.scheduler.eventbridge._create_schedule"):
        expected = [asyncio.run(deployed.schedule(*call)) for call in calls]
    assert got == expected
    out = " ".join(capsys.readouterr().out.split())  # Rich wraps at 80 columns
    assert f"[scheduler] SCHEDULE operation=order_followup delay=30m id={got[0]}" in out
    assert f"[scheduler] CANCEL id={got[0]}" in out


_NO_FILE = object()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("scheduler_lambda: false", ""),
        (None, ""),
        ("scheduler_lambda: true", DEV),
        (_NO_FILE, DEV),
    ],
)
def test_the_dev_command_sets_the_variable_from_the_yaml(
    monkeypatch, tmp_path, line, expected
) -> None:
    """cmd_dev sets SCHEDULER_LAMBDA_ARN before the agent, and with it the
    agent's socialagent_runtime config, is loaded. A value already in the
    shell does not win over the YAML."""
    import os  # noqa: PLC0415

    (tmp_path / "agent.py").write_text("def invoke(payload, context=None): ...\n")
    if line is not _NO_FILE:
        _yaml(tmp_path, line)
    # Through monkeypatch, so what cmd_dev sets is undone after the test.
    for key, value in dev._DEV_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "stale")
    seen: list[str] = []

    def _load(_path):
        seen.append(os.environ["SCHEDULER_LAMBDA_ARN"])
        raise SystemExit(0)

    monkeypatch.setattr(dev, "_load_entrypoint", _load)
    monkeypatch.setattr(
        "socialagent.cli.runtime_cmd.warn_if_stale", lambda _project_dir: None
    )
    args = MagicMock(
        entrypoint=str(tmp_path / "agent.py"),
        handlers=str(tmp_path / "handlers"),
        cloud=False,
        env="dev",
        phone=PHONE,
    )
    with pytest.raises(SystemExit):
        dev.cmd_dev(args)
    assert seen == [expected]
