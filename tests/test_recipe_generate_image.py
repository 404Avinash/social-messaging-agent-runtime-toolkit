# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The generate-image recipe must not upload a filtered/blank result as valid.

Stability signals a content-filter block in finish_reasons rather than as an
error, and may still return a null image. Decoding it anyway uploaded a blank
image WhatsApp then rejected or showed empty.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_RECIPE = _REPO / "recipes" / "generate-image" / "tool.py"
_BLUEPRINT_COPY = (
    _REPO / "blueprints" / "personal-assistant" / "src" / "tools" / "generate_image.py"
)


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("_generate_image_recipe", _RECIPE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bedrock_returning(payload: dict) -> MagicMock:
    client = MagicMock()
    client.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps(payload).encode())
    }
    return client


def _run_in_prod(gen, bedrock_payload, boto3_calls=None):
    """Invoke generate_image with prod config and a stubbed bedrock client.

    Pass a list as *boto3_calls* to collect the ``boto3.client`` calls made.
    """
    fake_config = SimpleNamespace(
        is_dev_mode=False, media_bucket="test-bucket", region="us-east-1"
    )
    with (
        patch.object(gen, "config", fake_config),
        patch.object(gen, "boto3") as boto3_mod,
    ):
        boto3_mod.client.return_value = _bedrock_returning(bedrock_payload)
        result = asyncio.run(gen.generate_image("a cat"))
    if boto3_calls is not None:
        boto3_calls.extend(boto3_mod.client.call_args_list)
    return result


def _bedrock_region(gen) -> str:
    """The Region generate_image creates its bedrock-runtime client in."""
    calls = []
    # A filtered result returns right after the model call, before any S3 work.
    _run_in_prod(gen, {"finish_reasons": ["CONTENT_FILTERED"]}, calls)
    regions = [
        c.kwargs.get("region_name") for c in calls if c.args == ("bedrock-runtime",)
    ]
    assert len(regions) == 1, f"expected one bedrock-runtime client: {calls}"
    return regions[0]


def test_content_filtered_result_is_an_error_not_an_upload(gen):
    result = _run_in_prod(
        gen, {"finish_reasons": ["CONTENT_FILTERED"], "images": [None]}
    )
    assert "error" in result
    assert "filter" in result["error"].lower()
    assert "image_id" not in result


def test_missing_image_is_an_error(gen):
    result = _run_in_prod(gen, {"finish_reasons": [None], "images": []})
    assert result.get("error")
    assert "image_id" not in result


def test_personal_assistant_ships_the_recipe_unchanged():
    """personal-assistant pre-wires the recipe, so its copy is the recipe.

    The copy drifted once: the filtered-image guard above landed in the recipe
    only, and the blueprint went on uploading blank images.
    """
    assert _BLUEPRINT_COPY.read_bytes() == _RECIPE.read_bytes(), (
        f"{_BLUEPRINT_COPY.relative_to(_REPO)} differs from "
        f"{_RECIPE.relative_to(_REPO)}; copy the recipe over it"
    )


def test_image_model_region_defaults_to_us_west_2(gen, monkeypatch):
    """Unset, the model is called in us-west-2, whatever the stack's Region."""
    monkeypatch.delenv("IMAGE_MODEL_REGION", raising=False)
    assert _bedrock_region(gen) == "us-west-2"


def test_empty_image_model_region_counts_as_unset(gen, monkeypatch):
    """An empty value (``IMAGE_MODEL_REGION: ""``) keeps the default, not region_name=""."""
    monkeypatch.setenv("IMAGE_MODEL_REGION", "")
    assert _bedrock_region(gen) == "us-west-2"


def test_image_model_region_chooses_where_the_model_is_called(gen, monkeypatch):
    """IMAGE_MODEL_REGION moves the Bedrock call; S3 stays in the stack's Region."""
    monkeypatch.setenv("IMAGE_MODEL_REGION", "eu-west-1")
    assert _bedrock_region(gen) == "eu-west-1"

    calls = []
    payload = {"finish_reasons": [None], "images": ["aGVsbG8="]}
    with patch.object(gen.notify, "upload_media", AsyncMock(return_value="mid")):
        result = _run_in_prod(gen, payload, calls)
    assert result == {"image_id": "mid", "prompt": "a cat"}
    s3_regions = [c.kwargs.get("region_name") for c in calls if c.args == ("s3",)]
    assert s3_regions == ["us-east-1"]


def test_runtime_role_may_invoke_foundation_models_in_any_region():
    """IMAGE_MODEL_REGION needs no IAM change only while the grant spans Regions.

    The runtime role allows bedrock:InvokeModel on foundation models in every
    Region. Narrowing it to the stack's Region would deny the default
    us-west-2 call from every stack deployed anywhere else.
    """
    tf = (_REPO / "infra-terraform" / "modules" / "iam" / "main.tf").read_text()
    match = re.search(
        r'Sid\s*=\s*"BedrockModelInvocation".*?Resource\s*=\s*\[(.*?)\]', tf, re.S
    )
    assert match, "no BedrockModelInvocation statement in the IAM module"
    assert '"arn:aws:bedrock:*::foundation-model/*"' in match.group(1)
