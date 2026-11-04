# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The webhook Lambda's limits in infra-terraform/modules/lambdas.

The webhook runs one agent turn per message, and a turn is not idempotent: run
twice, it replies twice and runs its tools twice. Two things keep a slow turn
from running again: the runtime call must give up before the Lambda times out,
so the handler can still answer, and Lambda must not replay the SNS event when
the function fails or times out anyway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from socialagent.constants import (
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_READ_TIMEOUT_SECONDS,
    RUNTIME_REPLY_RESERVE_SECONDS,
)

LAMBDAS_TF = (
    Path(__file__).parent.parent / "infra-terraform" / "modules" / "lambdas" / "main.tf"
)


def _resource(kind: str, name: str) -> str:
    tf = LAMBDAS_TF.read_text()
    start = tf.index(f'resource "{kind}" "{name}"')
    return tf[start : tf.index("\n}\n", start)]


def test_runtime_call_gives_up_before_the_webhook_times_out() -> None:
    body = _resource("aws_lambda_function", "webhook")
    timeout = int(re.search(r"^\s*timeout\s*=\s*(\d+)", body, re.M).group(1))
    waited = RUNTIME_CONNECT_TIMEOUT_SECONDS + RUNTIME_READ_TIMEOUT_SECONDS
    assert waited + RUNTIME_REPLY_RESERVE_SECONDS <= timeout, (
        f"the runtime call may wait {waited}s, which leaves the {timeout}s webhook "
        f"less than {RUNTIME_REPLY_RESERVE_SECONDS}s to send the reply or the "
        "timeout error"
    )


def test_webhook_async_invocations_are_not_retried() -> None:
    body = _resource("aws_lambda_function_event_invoke_config", "webhook")
    assert re.search(
        r"^\s*function_name\s*=\s*aws_lambda_function\.webhook\.function_name\s*$",
        body,
        re.M,
    )
    assert re.search(r"^\s*maximum_retry_attempts\s*=\s*0\s*$", body, re.M)


def test_minimal_deploy_policy_can_manage_the_async_invoke_config() -> None:
    """minimal-deploy-policy.json is the documented least-privilege deployer.
    The AWS provider creates and updates the webhook's async invoke config with
    Put, reads it with Get and destroys it with Delete, so without those an
    apply with a role built from that policy fails. Update is granted too: it
    allows nothing Put does not, and a provider release that calls it then
    does not fail the apply."""
    policy = json.loads(
        (LAMBDAS_TF.parents[2] / "minimal-deploy-policy.json").read_text()
    )
    granted: set[str] = set()
    for statement in policy["Statement"]:
        if statement["Effect"] == "Allow":
            actions = statement["Action"]
            granted.update([actions] if isinstance(actions, str) else actions)
    for verb in ("Put", "Get", "Update", "Delete"):
        action = f"lambda:{verb}FunctionEventInvokeConfig"
        assert action in granted, f"minimal-deploy-policy.json lacks {action}"
