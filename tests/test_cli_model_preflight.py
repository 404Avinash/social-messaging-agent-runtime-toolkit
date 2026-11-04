# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``socialagent deploy`` checks the project's Bedrock model is available in the
stack's Region before Terraform runs, so a model that is not enabled fails here with
one sentence instead of on the first WhatsApp message with an AccessDenied in a
Lambda log."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from socialagent.cli._deploy import check_model_available, project_model_id
from socialagent.cli._errors import CliError

_MODEL_PY = """
import os

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"
MODEL_ID = os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID
"""


def _project(tmp_path: Path, model_py: str | None = _MODEL_PY) -> Path:
    if model_py is not None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "model.py").write_text(model_py)
    return tmp_path


# --- which model the project will call --------------------------------------


def test_model_id_comes_from_agent_environment_first(tmp_path):
    config = {"agent": {"environment": {"MODEL_ID": "eu.anthropic.claude-haiku-5-5"}}}
    assert (
        project_model_id(config, _project(tmp_path)) == "eu.anthropic.claude-haiku-5-5"
    )


def test_model_id_falls_back_to_the_blueprint_default(tmp_path):
    assert project_model_id({}, _project(tmp_path)) == "us.anthropic.claude-haiku-5-5"


def test_empty_env_value_means_unset(tmp_path):
    config = {"agent": {"environment": {"MODEL_ID": ""}}}
    assert (
        project_model_id(config, _project(tmp_path)) == "us.anthropic.claude-haiku-5-5"
    )


def test_no_model_module_means_no_model(tmp_path):
    assert project_model_id({}, _project(tmp_path, model_py=None)) is None


def test_model_module_without_default_means_no_model(tmp_path):
    assert project_model_id({}, _project(tmp_path, model_py="x = 1\n")) is None


# --- the Bedrock check -------------------------------------------------------


def _not_found(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, op
    )


def test_inference_profile_found_passes():
    bedrock = MagicMock()
    bedrock.get_inference_profile.return_value = {"status": "ACTIVE"}
    check_model_available("us.anthropic.claude-haiku-5-5", "us-east-1", bedrock=bedrock)
    bedrock.get_inference_profile.assert_called_once_with(
        inferenceProfileIdentifier="us.anthropic.claude-haiku-5-5"
    )
    bedrock.get_foundation_model.assert_not_called()


def test_plain_model_id_checks_the_foundation_model():
    bedrock = MagicMock()
    bedrock.get_inference_profile.side_effect = _not_found("GetInferenceProfile")
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelLifecycle": {"status": "ACTIVE"}}
    }
    check_model_available(
        "anthropic.claude-haiku-4-5-20251001-v1:0", "us-east-1", bedrock=bedrock
    )
    bedrock.get_foundation_model.assert_called_once()


def test_unknown_model_is_a_clear_error_naming_model_and_region():
    bedrock = MagicMock()
    bedrock.get_inference_profile.side_effect = _not_found("GetInferenceProfile")
    bedrock.get_foundation_model.side_effect = _not_found("GetFoundationModel")
    with pytest.raises(CliError) as exc:
        check_model_available(
            "us.anthropic.claude-haiku-9", "eu-west-3", bedrock=bedrock
        )
    msg = str(exc.value)
    assert "us.anthropic.claude-haiku-9" in msg and "eu-west-3" in msg
    assert "MODEL_ID" in msg and "--skip-model-check" in msg
    assert "Model access" not in msg  # that console page no longer exists


def test_access_denied_on_the_check_itself_is_a_warning_not_a_failure(capsys):
    bedrock = MagicMock()
    bedrock.get_inference_profile.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "GetInferenceProfile",
    )
    check_model_available("us.anthropic.claude-haiku-5-5", "us-east-1", bedrock=bedrock)
    assert "could not verify" in capsys.readouterr().err.lower()


def test_legacy_profile_status_is_checked():
    bedrock = MagicMock()
    bedrock.get_inference_profile.return_value = {"status": "INACTIVE"}
    with pytest.raises(CliError):
        check_model_available(
            "us.anthropic.claude-haiku-5-5", "us-east-1", bedrock=bedrock
        )
