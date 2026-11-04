# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dev mode must be opted into, never inferred from missing configuration.

``is_dev_mode`` used to be true when ``NOTIFICATION_LAMBDA_ARN`` was the dev
sentinel **or empty**. An empty value in the AgentCore Runtime container therefore
switched the production agent onto the dev stubs: ``notify.send_*`` wrote to a
JSON file under ``/tmp`` and returned success, ``upload_media`` returned
``"dev-media-id"``, and ``scheduler.schedule`` only printed. Every outbound
message and schedule was dropped while the tools told the agent they had worked.

The missing-variable check that would have caught it was skipped for exactly the
same reason — it only runs when ``is_dev_mode`` is false.

AGENTS.md records that the Terraform provider sometimes drops
``environment_variables`` on a runtime update, so the trigger is not theoretical.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# socialagent-runtime files are importable by filename (vendored package pattern).
_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import config as agent_config_mod  # noqa: E402

_REAL_ARN = "arn:aws:lambda:us-east-1:111122223333:function:notify"

_ALL_VARS = (
    "NOTIFICATION_LAMBDA_ARN",
    "SCHEDULER_PREFIX",
    "SCHEDULER_ROLE_ARN",
    "MEDIA_BUCKET",
    "CONVERSATION_LOGS_BUCKET",
    "STATE_TABLE_NAME",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Start from no agent env vars at all, whatever the session set."""
    for key in _ALL_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_dev_sentinel_enables_dev_mode(clean_env):
    """The explicit opt-in `socialagent dev` uses still works."""
    clean_env.setenv("NOTIFICATION_LAMBDA_ARN", agent_config_mod.DEV_MODE_VALUE)
    assert agent_config_mod.AgentConfig.from_env().is_dev_mode is True


def test_empty_notification_arn_is_fatal(clean_env):
    """An empty ARN is a broken deployment, not a request for dev mode."""
    clean_env.setenv("NOTIFICATION_LAMBDA_ARN", "")
    with pytest.raises(ValueError, match="NOTIFICATION_LAMBDA_ARN"):
        agent_config_mod.AgentConfig.from_env()


def test_absent_notification_arn_is_fatal(clean_env):
    """Same when the variable is missing entirely rather than empty."""
    with pytest.raises(ValueError, match="NOTIFICATION_LAMBDA_ARN"):
        agent_config_mod.AgentConfig.from_env()


def test_the_error_names_the_dev_sentinel(clean_env):
    """Whoever trips this locally needs to be told how to ask for dev mode."""
    with pytest.raises(ValueError, match=agent_config_mod.DEV_MODE_VALUE):
        agent_config_mod.AgentConfig.from_env()


def test_real_arn_is_production_mode(clean_env):
    """A configured ARN means production: no stubs, no raise."""
    clean_env.setenv("NOTIFICATION_LAMBDA_ARN", _REAL_ARN)
    clean_env.setenv("SCHEDULER_PREFIX", "socialagent-dev")
    clean_env.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::111122223333:role/r")
    clean_env.setenv("MEDIA_BUCKET", "bucket")
    assert agent_config_mod.AgentConfig.from_env().is_dev_mode is False


def test_other_missing_vars_still_only_warn(clean_env):
    """Only the notification ARN is fatal — it is the one with no outbound path.

    A missing MEDIA_BUCKET degrades media handling; a missing NOTIFICATION_LAMBDA_ARN
    means nothing can leave at all, so the two do not deserve the same treatment.
    """
    clean_env.setenv("NOTIFICATION_LAMBDA_ARN", _REAL_ARN)
    cfg = agent_config_mod.AgentConfig.from_env()
    assert cfg.is_dev_mode is False
    assert cfg.media_bucket == ""
