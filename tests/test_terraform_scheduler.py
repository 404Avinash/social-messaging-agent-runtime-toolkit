# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Terraform wiring for schedules created from handler code.

``EventContext.schedule`` runs in the event and scheduler Lambdas. It targets
``SCHEDULER_LAMBDA_ARN`` (the only Lambda that runs ``@app.on_schedule``
handlers), passes ``SCHEDULER_ROLE_ARN`` to EventBridge Scheduler and names the
schedule under ``SCHEDULER_PREFIX``, the prefix the IAM grants are scoped to.
Missing any of them, a follow-up lands on the notification Lambda (which
rejects it), fails on an empty role, or is created outside the grant.

The scheduler Lambda's ``invoke_agent`` handler also runs a whole agent turn
inside one invocation, so its timeout and async retries are pinned here too.

Static checks over the ``.tf`` sources: no AWS credentials and no plan.
"""

from __future__ import annotations

import fnmatch
import inspect
import re
from pathlib import Path

import pytest

from socialagent.handlers.scheduled import firing_record_key
from socialagent.state.table import SocialAgentState

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra-terraform"
LAMBDAS_TF = INFRA / "modules" / "lambdas" / "main.tf"
TOOL_ROLES_TF = INFRA / "modules" / "iam" / "tool-roles.tf"
EVENTBRIDGE_PY = REPO_ROOT / "src" / "socialagent" / "scheduler" / "eventbridge.py"


def _code(text: str) -> str:
    """*text* without full-line ``#`` comments, which quote what they explain."""
    return re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)


def _block(text: str, header: str) -> str:
    """The top-level block that starts with *header*, up to its closing brace."""
    start = text.index(header)
    return text[start : text.index("\n}\n", start) + 2]


def _lambda(function: str) -> str:
    return _code(
        _block(LAMBDAS_TF.read_text(), f'resource "aws_lambda_function" "{function}"')
    )


def _env(function: str) -> dict[str, str]:
    """NAME -> expression for the variables one Lambda sets on top of common_env."""
    block = _lambda(function)
    env = block[block.index("environment {") :]
    return dict(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*$", env, re.M))


def _local(name: str) -> str:
    tf = _code(LAMBDAS_TF.read_text())
    match = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*$", tf, re.M)
    assert match, f"no local.{name} in modules/lambdas/main.tf"
    return match.group(1)


def _number(block: str, attribute: str) -> int:
    match = re.search(rf"^\s*{attribute}\s*=\s*(\d+)\s*$", block, re.M)
    assert match, f"no `{attribute} = <number>`"
    return int(match.group(1))


# =============================================================================
# Environment
# =============================================================================


@pytest.mark.parametrize("function", ["event", "scheduler"])
def test_handler_lambdas_get_the_scheduler_wiring(function: str) -> None:
    env = _env(function)
    for name in ("SCHEDULER_LAMBDA_ARN", "SCHEDULER_ROLE_ARN", "SCHEDULER_PREFIX"):
        assert name in env, (
            f"the {function} Lambda runs EventContext.schedule but gets no {name}"
        )
    assert env["SCHEDULER_ROLE_ARN"] == "var.scheduler_role_arn"
    # The SchedulerManage grant is scoped to schedule/default/${local.prefix}-*.
    assert env["SCHEDULER_PREFIX"] == "local.prefix"


def test_both_lambdas_target_the_same_scheduler_arn() -> None:
    assert _env("event")["SCHEDULER_LAMBDA_ARN"] == "local.scheduler_lambda_arn"
    assert _env("scheduler")["SCHEDULER_LAMBDA_ARN"] == "local.scheduler_lambda_arn"


def test_scheduler_arn_names_the_scheduler_lambda() -> None:
    """Empty without a scheduler Lambda (the SDK then falls back to the
    notification Lambda), else the ARN of the function this module creates."""
    expr = _local("scheduler_lambda_arn")
    assert expr.startswith("var.enable_scheduler_lambda ?")
    assert expr.endswith(': ""')
    name = re.search(r'function_name\s*=\s*"([^"]+)"', _lambda("scheduler")).group(1)
    assert f':function:{name}"' in expr


def test_scheduler_arn_is_known_at_plan_time() -> None:
    """The scheduler Lambda cannot read its own ARN (a cycle), and the module's
    data sources are no way round it: module.lambdas has a depends_on, which
    defers them to apply whenever a module it depends on changes (the runtime
    does on most deploys), so both Lambdas would show an environment diff."""
    expr = _local("scheduler_lambda_arn")
    assert "aws_lambda_function.scheduler" not in expr
    assert "data." not in expr


def test_scheduler_arn_matches_what_the_runtime_and_iam_use() -> None:
    """The agent runtime and the scheduler role name the same function."""
    main = _code((INFRA / "main.tf").read_text())
    runtime = re.search(r"^\s*SCHEDULER_LAMBDA_ARN\s*=\s*(.+)$", main, re.M).group(1)
    assert "function:${local.prefix}-scheduler" in runtime
    invoke = _code(
        _block(
            TOOL_ROLES_TF.read_text(),
            'resource "aws_iam_role_policy" "scheduler_invoke_lambda"',
        )
    )
    assert "function:${local.prefix}-scheduler" in invoke
    assert ":function:${local.prefix}-scheduler" in _local("scheduler_lambda_arn")


# =============================================================================
# IAM
# =============================================================================


def _event_handler_statement(sid: str) -> str:
    policy = _code(
        _block(
            TOOL_ROLES_TF.read_text(), 'resource "aws_iam_role_policy" "event_handler"'
        )
    )
    match = re.search(rf'Sid\s*=\s*"{sid}"', policy)
    assert match, f"event_handler policy has no {sid} statement"
    return policy[
        policy.rindex("[{", 0, match.start()) : policy.index("}]", match.start())
    ]


def test_handler_role_can_make_every_scheduler_call_the_sdk_makes() -> None:
    """The event and scheduler Lambdas share the event_handler role. The SDK
    upserts: create_schedule, then update_schedule on a ConflictException, so a
    handler that re-schedules an id it used before needs UpdateSchedule too."""
    calls = set(re.findall(r"client\.(\w+)_schedule\(", EVENTBRIDGE_PY.read_text()))
    assert {"create", "update", "delete"} <= calls
    statement = _event_handler_statement("SchedulerManage")
    for call in sorted(calls):
        action = f'"scheduler:{call.capitalize()}Schedule"'
        assert action in statement, f"event_handler role lacks {action}"
    assert "schedule/default/${local.prefix}-*" in statement


def test_handler_role_passes_only_the_scheduler_role() -> None:
    statement = _event_handler_statement("PassSchedulerRole")
    assert '"iam:PassRole"' in statement
    assert "aws_iam_role.scheduler.arn" in statement
    assert '"scheduler.amazonaws.com"' in statement


@pytest.mark.parametrize("function", ["event", "scheduler"])
def test_handler_lambdas_use_the_event_handler_role(function: str) -> None:
    assert re.search(r"^\s*role\s*=\s*var\.event_role_arn\b", _lambda(function), re.M)


def _notification_policy() -> str:
    return _code(
        _block(
            TOOL_ROLES_TF.read_text(),
            'resource "aws_iam_role_policy" "notification_service"',
        )
    )


def _dynamodb_actions(policy: str) -> list[str]:
    """The DynamoDB actions the statements in *policy* allow, in order."""
    lists = re.findall(r"\bAction\s*=\s*\[([^\]]*)\]", policy)
    return [a for body in lists for a in re.findall(r'"(dynamodb:\w+)"', body)]


def test_notification_role_can_write_a_firing_record() -> None:
    """Without the scheduler Lambda a schedule's built-in send targets the
    notification Lambda, which writes the firing record before it sends. With
    no grant the put is denied, the handler sends anyway, and a repeated
    delivery sends again."""
    policy = _notification_policy()
    match = re.search(r'Sid\s*=\s*"ScheduleFiringRecord"', policy)
    assert match, "the notification role cannot write the firing record"
    statement = policy[
        policy.rindex("? [{", 0, match.start()) : policy.index("}] : []", match.start())
    ]
    gate = policy[: match.start()].rsplit("\n", 2)[-2]
    assert 'var.enable_state_table && var.state_table_arn != ""' in gate
    assert _dynamodb_actions(statement) == ["dynamodb:PutItem"]
    assert "var.state_table_arn" in statement
    # Only the record's own partition keys, so the role reads and writes no
    # user's state: the key firing_record_key builds must match the pattern.
    leading = re.search(r'"dynamodb:LeadingKeys"\s*=\s*\["([^"]+)"\]', statement)
    assert leading and '"ForAllValues:StringLike"' in statement
    pk, _ = firing_record_key("525555550100-send_text-abcd1234", "2026-10-30T09:00:00Z")
    assert fnmatch.fnmatchcase(pk, leading.group(1))


def test_the_firing_record_needs_only_put_item() -> None:
    """The grant is PutItem alone: claim_once must stay one conditional put."""
    calls = set(
        re.findall(r"table\.(\w+)\(", inspect.getsource(SocialAgentState.claim_once))
    )
    assert calls == {"put_item"}


def test_notification_role_has_no_other_state_table_access() -> None:
    assert _dynamodb_actions(_notification_policy()) == ["dynamodb:PutItem"]


# =============================================================================
# Timeout and retries
# =============================================================================


def test_scheduler_timeout_covers_an_agent_turn() -> None:
    """invoke_agent waits for a whole agent turn, the same call the webhook
    makes, so it gets at least the webhook's timeout."""
    webhook = _number(_lambda("webhook"), "timeout")
    assert _number(_lambda("scheduler"), "timeout") >= webhook


def test_scheduler_async_invocations_are_not_retried() -> None:
    """EventBridge Scheduler invokes the Lambda asynchronously, and Lambda
    retries a failed or timed-out async event twice by default. Each retry
    re-runs the agent turn and re-sends what the first attempt delivered."""
    tf = _code(LAMBDAS_TF.read_text())
    header = 'resource "aws_lambda_function_event_invoke_config" "scheduler"'
    assert header in tf
    block = _block(tf, header)
    assert re.search(r"count\s*=\s*var\.enable_scheduler_lambda \? 1 : 0", block)
    assert "aws_lambda_function.scheduler[0].function_name" in block
    assert _number(block, "maximum_retry_attempts") == 0
