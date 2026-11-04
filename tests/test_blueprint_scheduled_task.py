# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""personal-assistant's scheduled task gives up on the agent before Lambda does.

``handle_invoke_agent`` waits for a whole agent turn inside one scheduler
Lambda invocation, then sends the result or, on failure, a fallback text.
botocore's defaults (60 s per read, several retries) can outlast the Lambda
timeout, and a killed invocation sends nothing. A retry re-runs the turn.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER = REPO_ROOT / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
LAMBDAS_TF = REPO_ROOT / "infra-terraform" / "modules" / "lambdas" / "main.tf"
PHONE = "+15551234567"


def _scheduler_lambda_timeout() -> int:
    tf = LAMBDAS_TF.read_text()
    start = tf.index('resource "aws_lambda_function" "scheduler"')
    block = tf[start : tf.index("\n}\n", start)]
    return int(re.search(r"^\s*timeout\s*=\s*(\d+)\s*$", block, re.M).group(1))


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/r")
    monkeypatch.setenv("MEMORY_ID", "m")
    clients = MagicMock()
    monkeypatch.setattr(boto3, "client", clients)
    spec = importlib.util.spec_from_file_location("_pa_scheduler_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    # Blueprints are copied file by file into new projects; keep them free of
    # __pycache__.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    clients.reset_mock()
    return module, clients


def _run_task(module) -> AsyncMock:
    ctx = AsyncMock()
    ctx.lambda_context = None  # not in Lambda: no time limit to fit
    data = {"phone": PHONE, "prompt": "tell me a joke", "schedule_id": "s-1"}
    asyncio.run(module.handle_invoke_agent(data, ctx))
    return ctx


def _agentcore_config(clients: MagicMock):
    calls = [c for c in clients.call_args_list if c.args[:1] == ("bedrock-agentcore",)]
    assert len(calls) == 1
    config = calls[0].kwargs.get("config")
    assert config is not None, "the AgentCore client keeps botocore's defaults"
    return config


def test_the_agent_call_ends_before_the_lambda_does(handler) -> None:
    module, clients = handler
    clients.return_value.invoke_agent_runtime.return_value = {
        "response": json.dumps({"result": "ok"}).encode()
    }
    _run_task(module)
    config = _agentcore_config(clients)
    # Room left for the fallback send after a read that timed out.
    budget = config.connect_timeout + config.read_timeout
    assert budget <= _scheduler_lambda_timeout() - 10


def test_the_agent_call_is_not_retried(handler) -> None:
    """A retry re-runs the agent turn: the user would get its result twice."""
    module, clients = handler
    clients.return_value.invoke_agent_runtime.return_value = {
        "response": json.dumps({"result": "ok"}).encode()
    }
    _run_task(module)
    assert _agentcore_config(clients).retries.get("total_max_attempts") == 1


def test_a_timed_out_turn_still_tells_the_user(handler) -> None:
    module, clients = handler
    clients.return_value.invoke_agent_runtime.side_effect = ReadTimeoutError(
        endpoint_url="https://bedrock-agentcore.us-east-1.amazonaws.com"
    )
    ctx = _run_task(module)
    ctx.send_text.assert_awaited_once()
    assert ctx.send_text.await_args.args[0] == PHONE
