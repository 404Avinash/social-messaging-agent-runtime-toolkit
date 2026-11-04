# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A stack without the opt-in scheduler Lambda is not reported as degraded.

The scheduler Lambda exists only when the project enables
``features.scheduler_lambda``, which the registry publishes as
``features.scheduler``; the default deploy has none. The console still probed
``<prefix>-scheduler`` on every stack, the probe failed on the missing
function, and every default stack showed "degraded" with a hint to check its
IAM role and KMS key. The Errors tab, log search, config update and nuclear
reset all read or wrote the missing function as well.
"""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

PREFIX = "dev-bot"


def _config(scheduler: bool | None) -> dict:
    config = {"project": "bot", "environment": "dev", "outputs": {"webhook_lambda": f"{PREFIX}-webhook"}}
    if scheduler is not None:
        config["features"] = {"scheduler": scheduler, "state_table": True, "events_api": True}
    return config


def _lambda_client(deployed: set[str]) -> MagicMock:
    """Invoke and configuration calls fail for a function that does not exist, as on AWS."""

    def missing(name, op):
        return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": name}}, op)

    def invoke(FunctionName, **_):
        if FunctionName not in deployed:
            raise missing(FunctionName, "Invoke")
        return {"StatusCode": 200}

    def get_configuration(FunctionName):
        if FunctionName not in deployed:
            raise missing(FunctionName, "GetFunctionConfiguration")
        return {"Environment": {"Variables": {}}}

    client = MagicMock()
    client.invoke.side_effect = invoke
    client.get_function_configuration.side_effect = get_configuration
    client.get_function.return_value = {"Configuration": {"LastModified": "2026-10-01"}}
    return client


@pytest.fixture
def stack_env(monkeypatch):
    """The AWS clients routes.stacks uses, with webhook/notification/event deployed."""
    from routes import stacks

    deployed = {f"{PREFIX}-{s}" for s in ("webhook", "notification", "event")}
    lam = _lambda_client(deployed)
    cw = MagicMock()
    cw.get_metric_data.return_value = {"MetricDataResults": [{"Values": []}] * 3}
    table = MagicMock()
    table.query.return_value = {"Items": []}
    table.get_item.return_value = {}
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "cw", cw)
    monkeypatch.setattr(stacks, "settings_table", table)
    monkeypatch.setattr(stacks, "codebuild", MagicMock())
    monkeypatch.setattr(stacks, "s3", MagicMock())
    monkeypatch.setattr(stacks.boto3, "client", lambda *a, **k: MagicMock())
    return lam


@pytest.mark.parametrize("scheduler", [False, None])
def test_default_stack_without_a_scheduler_is_healthy(stack_env, scheduler):
    from routes import stacks

    status = stacks._stack_status(_config(scheduler))

    assert [fn["name"] for fn in status["lambdas"]] == ["webhook", "notification", "event"]
    assert status["health"] == "healthy", status["healthDetail"]
    probed = {c.kwargs["FunctionName"] for c in stack_env.invoke.call_args_list}
    assert f"{PREFIX}-scheduler" not in probed


def test_a_stack_that_deploys_the_scheduler_still_probes_it(stack_env):
    from routes import stacks

    status = stacks._stack_status(_config(True))

    assert [fn["name"] for fn in status["lambdas"]] == ["webhook", "notification", "event", "scheduler"]
    # Here the function really is missing, so the probe failure is the right report.
    assert status["health"] == "degraded"


def test_errors_tab_skips_the_missing_scheduler_log_group(monkeypatch):
    from routes import stacks

    logs = MagicMock()
    logs.filter_log_events.return_value = {"events": []}
    monkeypatch.setattr(stacks, "logs", logs)
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: _config(False))

    stacks.handle_get_errors({"id": "bot-dev"}, {})

    groups = {c.kwargs["logGroupName"] for c in logs.filter_log_events.call_args_list}
    assert groups == {f"/aws/lambda/{PREFIX}-{s}" for s in ("webhook", "notification", "event")}


def test_config_update_reports_only_the_deployed_lambdas(monkeypatch):
    from routes import stacks

    lam = _lambda_client({f"{PREFIX}-{s}" for s in ("webhook", "notification", "event")})
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: _config(False))

    resp = stacks.handle_update_config({"id": "bot-dev"}, {"environmentVariables": {"LOG_LEVEL": "DEBUG"}})

    assert json.loads(resp["body"])["updated"] == [f"{PREFIX}-{s}" for s in ("webhook", "notification", "event")]
    looked_up = {c.kwargs["FunctionName"] for c in lam.get_function_configuration.call_args_list}
    assert f"{PREFIX}-scheduler" not in looked_up


def test_nuclear_reset_does_not_restart_a_missing_scheduler(monkeypatch):
    from routes import ops

    lam = _lambda_client({f"{PREFIX}-{s}" for s in ("webhook", "notification", "event")})
    monkeypatch.setattr(ops, "lambda_client", lam)
    monkeypatch.setattr(ops, "_get_registry", lambda _id: _config(False))
    monkeypatch.setattr(ops, "handle_delete_all_conversations", lambda *_: {"body": json.dumps({"deleted": 0})})

    resp = ops.handle_nuclear_reset({"id": "bot-dev"}, {})

    assert json.loads(resp["body"])["lambdas_restarted"] == ["webhook", "notification", "event"]
    looked_up = {c.kwargs["FunctionName"] for c in lam.get_function_configuration.call_args_list}
    assert f"{PREFIX}-scheduler" not in looked_up


def test_log_search_skips_the_missing_scheduler_log_group(monkeypatch):
    from routes import logs

    client = MagicMock()
    client.filter_log_events.return_value = {"events": []}
    monkeypatch.setattr(logs, "client", lambda *_: client)
    monkeypatch.setattr(logs, "get_registry", lambda _id: _config(False))

    logs.search_logs_impl({"stack": "bot-dev", "minutes": 60})

    groups = {c.kwargs["logGroupName"] for c in client.filter_log_events.call_args_list}
    assert groups == {f"/aws/lambda/{PREFIX}-{s}" for s in ("webhook", "notification", "event")}


@pytest.mark.parametrize("scheduler", [False, None])
def test_metrics_probe_no_missing_scheduler(stack_env, monkeypatch, scheduler):
    from routes import stacks

    monkeypatch.setattr(stacks, "_get_registry", lambda _id: _config(scheduler))

    resp = stacks.handle_get_metrics({"id": "bot-dev"}, {})

    assert [fn["name"] for fn in json.loads(resp["body"])["lambdas"]] == ["webhook", "notification", "event"]
    probed = {c.kwargs["FunctionName"] for c in stack_env.invoke.call_args_list}
    assert f"{PREFIX}-scheduler" not in probed


def test_metrics_still_probe_a_deployed_scheduler(stack_env, monkeypatch):
    from routes import stacks

    monkeypatch.setattr(stacks, "_get_registry", lambda _id: _config(True))

    resp = stacks.handle_get_metrics({"id": "bot-dev"}, {})

    assert [fn["name"] for fn in json.loads(resp["body"])["lambdas"]] == ["webhook", "notification", "event", "scheduler"]
