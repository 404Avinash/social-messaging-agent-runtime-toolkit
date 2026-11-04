# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SocialAgentConfig: construction, from_env(), validation.
"""

import re
from pathlib import Path

import pytest

from socialagent.config import SocialAgentConfig


def test_explicit_construction() -> None:
    config = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-1",
        provider="eum_social",
        phone_number_id="ph-1",
    )
    assert config.region == "us-east-1"
    assert config.meta_api_version == "v20.0"
    assert config.error_messages == {}


def test_from_env_all_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc"
    )
    monkeypatch.setenv("MEMORY_ID", "mem-abc")
    monkeypatch.setenv("SOCIALAGENT_PROVIDER", "eum_social")
    monkeypatch.setenv("PHONE_NUMBER_ID", "ph-1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config = SocialAgentConfig.from_env()
    assert (
        config.agent_runtime_arn
        == "arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc"
    )
    assert config.memory_id == "mem-abc"
    assert config.provider == "eum_social"
    assert config.phone_number_id == "ph-1"
    assert config.region == "us-west-2"


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.delenv("SOCIALAGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    config = SocialAgentConfig.from_env()
    assert config.provider == "eum_social"
    assert config.region == "us-east-1"


def test_from_env_missing_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_ARN", raising=False)
    monkeypatch.delenv("MEMORY_ID", raising=False)
    with pytest.raises(ValueError, match="AGENT_RUNTIME_ARN"):
        SocialAgentConfig.from_env()


def test_from_env_missing_memory_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.delenv("MEMORY_ID", raising=False)
    with pytest.raises(ValueError, match="MEMORY_ID"):
        SocialAgentConfig.from_env()


def test_from_env_infra_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("NOTIFICATION_SERVICE_ARN", "arn:lambda:notify")
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:iam:scheduler")
    monkeypatch.setenv("STATE_TABLE_NAME", "my-table")
    config = SocialAgentConfig.from_env()
    assert config._notification_lambda_arn == "arn:lambda:notify"
    assert config._scheduler_role_arn == "arn:iam:scheduler"
    assert config._state_table_name == "my-table"


@pytest.mark.parametrize(("value", "expected"), [(None, 3.0), ("", 3.0), ("1.5", 1.5)])
def test_outbound_gap_seconds_from_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: float
) -> None:
    """Terraform always sets OUTBOUND_GAP_SECONDS; unset or empty keeps the default."""
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("PHONE_NUMBER_ID", "ph-x")
    if value is None:
        monkeypatch.delenv("OUTBOUND_GAP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("OUTBOUND_GAP_SECONDS", value)
    assert SocialAgentConfig.from_env().outbound_gap_seconds == expected


def test_terraform_rejects_a_zero_outbound_gap() -> None:
    """At 0 the queued attachments and the reply go out back to back, and
    WhatsApp can deliver them out of order — the validation must refuse it."""
    tf = (Path(__file__).parent.parent / "infra-terraform" / "variables.tf").read_text()
    block = tf[tf.index('variable "outbound_gap_seconds"') :]
    block = block[: block.index("\n}\n")]
    lower = re.search(r"var\.outbound_gap_seconds\s*(>=|>)\s*([0-9.]+)", block)
    assert lower, "outbound_gap_seconds has no lower bound"
    op, bound = lower.group(1), float(lower.group(2))
    assert bound > 0 or op == ">", f"the validation still accepts 0 ({op} {bound:g})"


def test_provider_receives_the_configured_gap() -> None:
    from socialagent.app import _make_provider

    config = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        outbound_gap_seconds=0.75,
    )
    assert _make_provider(config)._message_gap_seconds == 0.75
