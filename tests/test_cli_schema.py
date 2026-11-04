# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema v3 of socialagent.<env>.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from socialagent.cli import _schema
from socialagent.cli._errors import ConfigError
from socialagent.cli.config_file import load

REPO_ROOT = Path(__file__).resolve().parents[1]
_TF_VARS = set(
    re.findall(
        r'variable "(\w+)"',
        (REPO_ROOT / "infra-terraform" / "variables.tf").read_text(),
    )
)

_VALID = {
    "schema_version": 3,
    "project": "my-bot",
    "environment": "dev",
    "region": "us-east-1",
    "deployment_type": "docker",
    "whatsapp": {"phone_number_id": "arn:aws:social-messaging:us-east-1:1:phone/x"},
    "error_messages": {"generic": "Disculpa."},
    "features": {"state_table": True},
    "agent": {"environment": {"LOG": "1"}},
    "secrets": ["partner_api"],
    "terraform": {"log_retention_days": 14},
    "state": {"prefix": "legacy"},
    "outputs": {},
}


def test_every_first_class_field_is_a_real_terraform_variable():
    unknown = {v for v in _schema.FIRST_CLASS.values() if v not in _TF_VARS}
    assert not unknown, f"FIRST_CLASS maps to variables Terraform lacks: {unknown}"
    assert _schema.SPECIAL_VARIABLES <= _TF_VARS


def test_first_class_fields_map_to_distinct_variables():
    values = list(_schema.FIRST_CLASS.values())
    assert len(values) == len(set(values))


def test_valid_config_has_no_errors():
    assert _schema.validate(_VALID) == []


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ({"project": ""}, "missing required field: project"),
        ({"project": "My_Bot"}, "project must be"),
        ({"environment": "staging"}, "environment must be dev/test/prod"),
        ({"deployment_type": "container"}, "deployment_type must be"),
        ({"schema_version": 4}, "newer than this CLI supports"),
        ({"log_retention_days": 14}, "unknown field: log_retention_days"),
        ({"whatsapp": {"phone": "x"}}, "unknown field: whatsapp.phone"),
        ({"agent": {"model_id": "x"}}, "unknown field: agent.model_id"),
        ({"features": {"state_table": "yes"}}, "must be true or false"),
        ({"secrets": {"partner_api": {"key": "v"}}}, "must be a list of secret names"),
        ({"secrets": ["bad name!"]}, "not a valid secret name"),
        ({"terraform": {"aws_region": "x"}}, "duplicates `region`"),
        ({"terraform": {"secrets": {}}}, "list secret names under `secrets:`"),
        ({"state": {"bucket": "x"}}, "unknown field: state.bucket"),
        ({"outputs": {"runtime_arn": "arn:gcp:x"}}, "invalid ARN"),
    ],
)
def test_invalid_configs_are_reported(mutation, needle):
    errors = _schema.validate({**_VALID, **mutation})
    assert any(needle in e for e in errors), errors


def _tf_reserved_prefix() -> str:
    """The prefix variables.tf's agent_environment_variables validation refuses."""
    text = (REPO_ROOT / "infra-terraform" / "variables.tf").read_text()
    block = text[text.index('variable "agent_environment_variables"') :]
    return re.search(r'!startswith\(k, "([A-Z_]+)"\)', block).group(1)


def test_agent_environment_reserves_the_prefix_terraform_does():
    """The CLI and the plan refuse the same keys, so a file the CLI accepts
    never fails the plan on this, and the CLI says so first."""
    assert _schema._RESERVED_ENV_PREFIX == _tf_reserved_prefix()


def test_guardrail_keys_in_agent_environment_point_at_terraform_guardrail():
    env = {
        "BEDROCK_GUARDRAIL_ID": "gr-1",
        "BEDROCK_GUARDRAIL_VERSION": "2",
        "BEDROCK_GUARDRAIL_TRACE": "on",
        # Not reserved: the prefix must start the key.
        "MY_BEDROCK_GUARDRAIL_ID": "x",
        "BEDROCK_MODEL_HINT": "x",
    }
    errors = _schema.validate({**_VALID, "agent": {"environment": env}})
    assert [e.split(" ", 1)[0] for e in errors] == [
        "agent.environment.BEDROCK_GUARDRAIL_ID",
        "agent.environment.BEDROCK_GUARDRAIL_TRACE",
        "agent.environment.BEDROCK_GUARDRAIL_VERSION",
    ]
    for error in errors:
        assert "terraform.guardrail and terraform.guardrail_version" in error
        # Terraform sets only these two, so the message for any other reserved
        # key must not say Terraform sets that key.
        assert (
            "Terraform sets BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION" in error
        )


def test_load_refuses_a_guardrail_key_in_agent_environment(tmp_path):
    """Every command reads the file through load(), so this fails before a plan."""
    p = tmp_path / "socialagent.dev.yaml"
    config = {**_VALID, "agent": {"environment": {"BEDROCK_GUARDRAIL_ID": "gr-1"}}}
    p.write_text(yaml.safe_dump(config))
    with pytest.raises(ConfigError, match=r"agent\.environment\.BEDROCK_GUARDRAIL_ID"):
        load(p)


def test_upgrade_v2_moves_phone_number_id_and_keeps_the_rest():
    v2 = {
        "schema_version": 2,
        "project": "my-bot",
        "region": "us-east-1",
        "environment": "dev",
        "deployment_type": "docker",
        "phone_number_id": "arn:aws:x",
        "error_messages": {"generic": "hola"},
        "outputs": {"memory_id": "m"},
    }
    v3 = _schema.upgrade(v2)
    assert v3["schema_version"] == 3
    assert "phone_number_id" not in v3
    assert v3["whatsapp"] == {"phone_number_id": "arn:aws:x"}
    assert v3["error_messages"] == {"generic": "hola"}
    assert v3["outputs"] == {"memory_id": "m"}
    assert _schema.validate(v3) == []
    assert v2["phone_number_id"] == "arn:aws:x"  # input not mutated


def test_load_reads_a_v2_file_as_v3(tmp_path):
    p = tmp_path / "socialagent.dev.yaml"
    p.write_text(
        "schema_version: 2\nproject: my-bot\nregion: us-east-1\n"
        'environment: dev\nphone_number_id: ""\noutputs: {}\n'
    )
    cfg = load(p)
    assert cfg["schema_version"] == 3
    assert "phone_number_id" not in cfg


def test_load_names_the_file_and_every_problem(tmp_path):
    p = tmp_path / "socialagent.dev.yaml"
    p.write_text(yaml.safe_dump({**_VALID, "environment": "qa", "extra": 1}))
    with pytest.raises(ConfigError) as exc:
        load(p)
    msg = str(exc.value)
    assert "socialagent.dev.yaml" in msg
    assert "environment must be" in msg and "unknown field: extra" in msg


@pytest.mark.parametrize(
    "blueprint",
    sorted(p.name for p in (REPO_ROOT / "blueprints").iterdir() if p.is_dir()),
)
def test_blueprint_configs_are_valid_v3(blueprint):
    text = (REPO_ROOT / "blueprints" / blueprint / "socialagent.dev.yaml").read_text()
    cfg = yaml.safe_load(text.replace("__project_name__", "my-bot"))
    assert cfg["schema_version"] == 3
    assert _schema.validate(cfg) == []


_BLUEPRINTS = sorted(p.name for p in (REPO_ROOT / "blueprints").iterdir() if p.is_dir())
_GUARDRAIL_MESSAGES = (
    "guardrail_blocked_input_message",
    "guardrail_blocked_output_message",
)


def _blueprint_terraform(blueprint: str) -> dict:
    """The ``terraform:`` block of *blueprint*'s ``socialagent.dev.yaml``."""
    path = REPO_ROOT / "blueprints" / blueprint / "socialagent.dev.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("terraform") or {}


@pytest.mark.parametrize("blueprint", _BLUEPRINTS)
def test_blueprint_terraform_keys_are_root_variables(blueprint):
    # `deploy` refuses a key that is not a variable in variables.tf, so a
    # misspelt one would fail a scaffold's first deploy.
    unknown = set(_blueprint_terraform(blueprint)) - _TF_VARS
    assert not unknown, f"{blueprint}: not Terraform variables: {sorted(unknown)}"


def test_only_the_personal_assistant_sets_the_guardrail_messages():
    """The personal-assistant's templates and reminders are Spanish, so its
    YAML sets the guardrail's blocked messages in Spanish, the ones
    docs/configuration.md shows; the other blueprints keep the English
    defaults in variables.tf."""
    docs = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    for blueprint in _BLUEPRINTS:
        terraform = _blueprint_terraform(blueprint)
        messages = {k: terraform[k] for k in _GUARDRAIL_MESSAGES if k in terraform}
        if blueprint != "personal-assistant":
            assert not messages, f"{blueprint} overrides the English defaults"
            continue
        assert set(messages) == set(_GUARDRAIL_MESSAGES)
        for name, message in messages.items():
            assert 1 <= len(message) <= 500, name
            assert message.startswith("Lo siento"), name
            assert f'{name}: "{message}"' in docs, f"{name} differs from the docs"
