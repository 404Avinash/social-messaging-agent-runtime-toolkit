# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The personal-assistant scheduler's ``invoke_agent`` task runs the agent once.

EventBridge Scheduler invokes the scheduler Lambda asynchronously, and a
scheduled turn is not idempotent: its tools and sends run again on every
attempt. With botocore's defaults (a 60 s read timeout, legacy retries that
retry it) inside a 30 s Lambda, a slow task was killed mid-call and Lambda's
async retries ran it twice more, while the fallback text never went out.
Turning those retries off must not stop a throttled call from being tried
again, since it ran no turn: tasks due at the same minute fire together, and
AgentCore may throttle part of the burst.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from socialagent.constants import (
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_READ_TIMEOUT_SECONDS,
    RUNTIME_REPLY_RESERVE_SECONDS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = (
    REPO_ROOT / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
)
LAMBDAS_TF = REPO_ROOT / "infra-terraform" / "modules" / "lambdas" / "main.tf"
_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc"


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch):
    """The blueprint module, loaded the way its Lambda loads it (env set first)."""
    monkeypatch.setenv("AGENT_RUNTIME_ARN", _ARN)
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Requests are signed before ``before-send`` fires; nothing leaves the host.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    spec = importlib.util.spec_from_file_location("pa_scheduler", SCHEDULER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _LambdaClock:
    def __init__(self, seconds_left: float) -> None:
        self.left = seconds_left

    def get_remaining_time_in_millis(self) -> int:
        return int(self.left * 1000)


def _ctx(lambda_context: object = None) -> MagicMock:
    ctx = MagicMock()
    ctx.send_text = AsyncMock()
    ctx.send_template = AsyncMock()
    ctx.lambda_context = lambda_context
    return ctx


async def _no_sleep(_seconds: float) -> None:
    return None


def _answer(text: str) -> dict:
    return {"response": io.BytesIO(json.dumps({"result": text}).encode())}


def _throttled() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "InvokeAgentRuntime",
    )


def _clients_that_time_out(attempts: list[str], created: list) -> object:
    """Wrap boto3.client so every InvokeAgentRuntime send hits a read timeout."""
    real_client = boto3.client

    def client(*args, **kwargs):
        c = real_client(*args, **kwargs)

        def on_send(request, **_):
            attempts.append(request.url)
            raise ReadTimeoutError(endpoint_url=request.url)

        c.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", on_send
        )
        created.append(c)
        return c

    return client


def test_slow_scheduled_task_runs_once_and_sends_the_fallback(scheduler) -> None:
    attempts: list[str] = []
    created: list = []
    ctx = _ctx()
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with patch.object(boto3, "client", _clients_that_time_out(attempts, created)):
        asyncio.run(scheduler.handle_invoke_agent(data, ctx))

    assert len(attempts) == 1
    (client,) = created
    assert client.meta.config.connect_timeout == RUNTIME_CONNECT_TIMEOUT_SECONDS
    assert client.meta.config.read_timeout == RUNTIME_READ_TIMEOUT_SECONDS
    ctx.send_text.assert_awaited_once_with(
        "+525555550100", "⚠️ No pude completar la tarea programada."
    )


def test_scheduler_lambda_outlasts_the_runtime_wait() -> None:
    """The Lambda must still be alive to send the result, or the fallback."""
    tf = LAMBDAS_TF.read_text()
    start = tf.index('resource "aws_lambda_function" "scheduler"')
    body = tf[start : tf.index("\n}\n", start)]
    timeout = int(re.search(r"^\s*timeout\s*=\s*(\d+)", body, re.M).group(1))
    waited = RUNTIME_CONNECT_TIMEOUT_SECONDS + RUNTIME_READ_TIMEOUT_SECONDS
    assert waited + RUNTIME_REPLY_RESERVE_SECONDS <= timeout, (
        f"an invoke_agent task may wait {waited}s for the agent, which leaves the "
        f"{timeout}s scheduler Lambda too little time to send its result"
    )


def test_documented_reminders_carry_the_text_where_the_handler_reads_it() -> None:
    """A copied example sent ``data={"text": ...}``, and the reminder handler,
    which reads ``message``, sent its default text instead."""
    handler = SCHEDULER.read_text()
    reminder = handler[handler.index('@app.on_schedule("reminder")') :]
    assert 'data.get("message"' in reminder.split("\n\n\n")[0]

    docs = "\n".join(p.read_text() for p in sorted((REPO_ROOT / "docs").glob("*.md")))
    calls = re.findall(r'operation="reminder",(.*?)\n\s*\)', docs, re.S)
    assert len(calls) >= 4
    for call in calls:
        keys = re.findall(r'data=\{"(\w+)"', call)
        assert keys in ([], ["message"]), call


def test_a_throttled_scheduled_task_is_tried_again(scheduler) -> None:
    """A throttled call ran no turn, so trying it again runs the task once."""
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = [_throttled(), _answer("Listo.")]
    ctx = _ctx()
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with (
        patch.object(boto3, "client", MagicMock(return_value=agentcore)),
        patch.object(asyncio, "sleep", _no_sleep),
    ):
        asyncio.run(scheduler.handle_invoke_agent(data, ctx))

    assert agentcore.invoke_agent_runtime.call_count == 2
    assert ctx.send_template.await_args.args[1] == "task_completed"
    assert ctx.send_template.await_args.args[2] == ["Resume mis pendientes", "Listo."]
    ctx.send_text.assert_not_awaited()


def test_a_retried_task_that_then_runs_slow_is_not_run_again(scheduler) -> None:
    """After a throttled try, the next one reached the agent: its read timeout
    may come after the turn started, so it is not sent a third time."""
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = [
        _throttled(),
        ReadTimeoutError(endpoint_url="https://bedrock-agentcore.example"),
    ]
    ctx = _ctx()
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with (
        patch.object(boto3, "client", MagicMock(return_value=agentcore)),
        patch.object(asyncio, "sleep", _no_sleep),
    ):
        asyncio.run(scheduler.handle_invoke_agent(data, ctx))

    assert agentcore.invoke_agent_runtime.call_count == 2
    ctx.send_template.assert_not_awaited()
    ctx.send_text.assert_awaited_once_with(
        "+525555550100", "⚠️ No pude completar la tarea programada."
    )


def test_a_task_throttled_every_time_sends_the_fallback(scheduler) -> None:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = _throttled()
    ctx = _ctx()
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with (
        patch.object(boto3, "client", MagicMock(return_value=agentcore)),
        patch.object(asyncio, "sleep", _no_sleep),
    ):
        asyncio.run(scheduler.handle_invoke_agent(data, ctx))

    assert agentcore.invoke_agent_runtime.call_count == 3
    ctx.send_text.assert_awaited_once_with(
        "+525555550100", "⚠️ No pude completar la tarea programada."
    )


def test_the_scheduled_wait_fits_the_time_the_lambda_has_left(scheduler) -> None:
    """A fixed 100 s wait left a task that started late no time to send."""
    attempts: list[str] = []
    created: list = []
    ctx = _ctx(_LambdaClock(60))
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with patch.object(boto3, "client", _clients_that_time_out(attempts, created)):
        asyncio.run(scheduler.handle_invoke_agent(data, ctx))

    (client,) = created
    assert client.meta.config.read_timeout == pytest.approx(
        60 - RUNTIME_CONNECT_TIMEOUT_SECONDS - RUNTIME_REPLY_RESERVE_SECONDS
    )
    assert len(attempts) == 1
    ctx.send_text.assert_awaited_once_with(
        "+525555550100", "⚠️ No pude completar la tarea programada."
    )


def test_a_retried_task_logs_the_user_masked(scheduler, caplog) -> None:
    """The retry's log line names the user only by the last four digits."""
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = [_throttled(), _answer("Listo.")]
    data = {"phone": "+525555550100", "prompt": "Resume mis pendientes"}
    with (
        patch.object(boto3, "client", MagicMock(return_value=agentcore)),
        patch.object(asyncio, "sleep", _no_sleep),
        caplog.at_level("WARNING"),
    ):
        asyncio.run(scheduler.handle_invoke_agent(data, _ctx()))

    retried = [
        r.getMessage() for r in caplog.records if "trying again" in r.getMessage()
    ]
    assert retried, "the throttled try was not logged"
    assert all("5555550100" not in line and "0100" in line for line in retried)
