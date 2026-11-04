# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""socialagent secrets set against a moto Secrets Manager."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from socialagent.cli._errors import CliError
from socialagent.cli.secrets_cmd import cmd_secrets_set, secret_id

REPO_ROOT = Path(__file__).resolve().parents[1]


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(
        **{"name": "partner_api", "env": "dev", "key": [], "from_file": "", **kw}
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    (tmp_path / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: us-east-1\n"
        "secrets: [partner_api]\n"
    )
    return tmp_path


@pytest.fixture
def creds(tmp_path_factory):
    """A directory outside the project: where credential files belong."""
    return tmp_path_factory.mktemp("creds")


@pytest.fixture
def sm():
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        # What Terraform creates: the secret, seeded with {}.
        client.create_secret(Name="dev-my-bot-partner_api", SecretString="{}")
        yield client


def test_secret_id_matches_the_terraform_module():
    tf = (REPO_ROOT / "infra-terraform" / "modules" / "secrets" / "main.tf").read_text()
    assert 'prefix = "${var.environment}-${var.project_name}"' in tf
    assert 'name                    = "${local.prefix}-${each.key}"' in tf
    assert secret_id({"project": "my-bot", "environment": "dev"}, "x") == "dev-my-bot-x"


def test_terraform_ignores_later_value_changes():
    tf = (REPO_ROOT / "infra-terraform" / "modules" / "secrets" / "main.tf").read_text()
    assert "ignore_changes = [secret_string]" in tf


def test_set_from_file(project, creds, sm, capsys):
    (creds / "v.json").write_text(json.dumps({"api_key": "k1", "api_secret": "s1"}))
    cmd_secrets_set(_args(from_file=str(creds / "v.json")), client=sm)
    stored = json.loads(
        sm.get_secret_value(SecretId="dev-my-bot-partner_api")["SecretString"]
    )
    assert stored == {"api_key": "k1", "api_secret": "s1"}
    out = capsys.readouterr().out
    assert "api_key, api_secret" in out and "k1" not in out and "s1" not in out


def test_set_with_prompted_keys(project, sm, monkeypatch):
    monkeypatch.setattr(
        "getpass.getpass", lambda prompt: f"value-of-{prompt.rstrip(': ')}"
    )
    cmd_secrets_set(_args(key=["api_key"]), client=sm)
    stored = json.loads(
        sm.get_secret_value(SecretId="dev-my-bot-partner_api")["SecretString"]
    )
    assert stored == {"api_key": "value-of-api_key"}


def test_undeclared_secret_is_refused_before_any_aws_call(project):
    with pytest.raises(CliError, match="not listed under `secrets:`"):
        cmd_secrets_set(_args(name="other", key=["k"]), client=object())


def test_secret_not_yet_deployed(project, creds):
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        (creds / "v.json").write_text('{"k": "v"}')
        with pytest.raises(CliError, match="deploy the stack first"):
            cmd_secrets_set(_args(from_file=str(creds / "v.json")), client=client)


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("not json", "not valid JSON"),
        ('{"k": 1}', "JSON object of strings"),
        ("{}", "empty"),
        ('{"k": "  "}', "empty value for k"),
    ],
)
def test_bad_values_are_refused(project, creds, content, needle):
    (creds / "v.json").write_text(content)
    with pytest.raises(CliError, match=needle):
        cmd_secrets_set(_args(from_file=str(creds / "v.json")), client=object())


def test_needs_a_value_source(project):
    with pytest.raises(CliError, match="--key"):
        cmd_secrets_set(_args(), client=object())


def _stored(sm) -> dict:
    return json.loads(
        sm.get_secret_value(SecretId="dev-my-bot-partner_api")["SecretString"]
    )


def test_rotating_one_key_keeps_the_others(project, sm, monkeypatch, capsys):
    """`secrets set <name> --key api_key` is the documented way to rotate a
    value; it must not delete the secret's other keys."""
    sm.put_secret_value(
        SecretId="dev-my-bot-partner_api",
        SecretString=json.dumps({"api_key": "k1", "api_secret": "s1"}),
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: "k2")
    cmd_secrets_set(_args(key=["api_key"]), client=sm)

    assert _stored(sm) == {"api_key": "k2", "api_secret": "s1"}
    out = capsys.readouterr().out
    assert "api_key (kept api_secret)" in out and "k2" not in out


def test_an_empty_answer_writes_nothing(project, sm, monkeypatch):
    sm.put_secret_value(
        SecretId="dev-my-bot-partner_api", SecretString=json.dumps({"api_key": "k1"})
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    with pytest.raises(CliError, match="empty value for api_key"):
        cmd_secrets_set(_args(key=["api_key"]), client=sm)
    assert _stored(sm) == {"api_key": "k1"}


def test_key_update_needs_an_object_to_update(project, sm, monkeypatch):
    sm.put_secret_value(SecretId="dev-my-bot-partner_api", SecretString="plain")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "v")
    with pytest.raises(CliError, match="does not hold a JSON object"):
        cmd_secrets_set(_args(key=["api_key"]), client=sm)


def test_credentials_file_inside_the_project_is_refused(project):
    """The agent-source archive ships the project directory, so a credentials
    file there reaches S3 on the next deploy."""
    (project / "creds.json").write_text('{"k": "v"}')
    with pytest.raises(CliError, match="inside the project"):
        cmd_secrets_set(_args(from_file="creds.json"), client=object())


def test_from_stdin(project, sm, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO('{"api_key": "k3"}'))
    cmd_secrets_set(_args(from_file="-"), client=sm)
    assert _stored(sm) == {"api_key": "k3"}


def test_event_and_scheduler_handlers_can_read_secrets():
    """Project code runs in the webhook (hooks), the runtime and the event and
    scheduler handlers; all of them must be granted read. The notification
    Lambda runs only SDK code and is deliberately not."""
    module = (
        REPO_ROOT / "infra-terraform" / "modules" / "secrets" / "main.tf"
    ).read_text()
    root = (REPO_ROOT / "infra-terraform" / "main.tf").read_text()
    assert 'resource "aws_iam_role_policy" "handler_secrets"' in module
    assert "handler_role_name = module.iam.event_handler_role_name" in root
    assert "notification" not in root[root.index('module "secrets"') :].split("}")[0]
