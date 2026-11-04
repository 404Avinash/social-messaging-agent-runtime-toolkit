# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The personal-assistant webhook calls its model through Bedrock Converse.

Converse has one request shape for every Bedrock model, so the image and PDF
paths work with whatever MODEL_ID names, not only Claude. The helper reads the
answer by block key (models that reason open with ``reasoningContent``) and
raises when there is no usable text, which sends the webhook down its existing
fallback path. With the stack's guardrail in the environment, every call
passes it, so an intervention takes that fallback path too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

HANDLERS = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "personal-assistant"
    / "handlers"
)


@pytest.fixture
def bedrock_helper(monkeypatch):
    """Import handlers/shared/bedrock.py as the handler Lambda does (``shared.*``)."""
    monkeypatch.syspath_prepend(str(HANDLERS))
    for name in [m for m in sys.modules if m == "shared" or m.startswith("shared.")]:
        monkeypatch.delitem(sys.modules, name)
    import shared.bedrock as helper

    yield helper
    for name in [m for m in sys.modules if m == "shared" or m.startswith("shared.")]:
        sys.modules.pop(name, None)


def _converse_response(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


def test_skips_reasoning_blocks(bedrock_helper):
    response = _converse_response(
        [
            {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}},
            {"text": "A red car "},
            {"text": "parked outside."},
        ]
    )
    assert bedrock_helper.response_text(response) == "A red car parked outside."


@pytest.mark.parametrize(
    "response",
    [
        _converse_response(
            [{"text": "Sorry, I can't help with that."}], "guardrail_intervened"
        ),
        _converse_response([{"text": "blocked"}], "content_filtered"),
        _converse_response(
            [{"reasoningContent": {"reasoningText": {"text": ""}}}], "max_tokens"
        ),
        _converse_response([]),
    ],
    ids=["guardrail", "content-filter", "cut-off-while-reasoning", "empty"],
)
def test_raises_without_usable_text(bedrock_helper, response):
    with pytest.raises(bedrock_helper.NoTextResponse):
        bedrock_helper.response_text(response)


def _client():
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="testing",  # pragma: allowlist secret
        aws_secret_access_key="testing",  # pragma: allowlist secret
    )


# The two requests the webhook makes: an image to describe, and a PDF to
# summarize with citations enabled.
_REQUESTS = pytest.mark.parametrize(
    "content",
    [
        [
            {"image": {"format": "jpeg", "source": {"bytes": b"\xff\xd8fake"}}},
            {"text": "Describe this image."},
        ],
        [
            {
                "document": {
                    "format": "pdf",
                    "name": "document",
                    "source": {"bytes": b"%PDF-1.7"},
                    "citations": {"enabled": True},
                }
            },
            {"text": "Summarize this document."},
        ],
    ],
    ids=["image", "pdf"],
)


@_REQUESTS
def test_requests_match_the_converse_api(bedrock_helper, content, monkeypatch):
    """botocore validates each request against the Converse API model, offline.

    The guardrail id is empty, as Terraform sets it with ``guardrail: none``,
    and the expected request has no ``guardrailConfig``.
    """
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "")
    client = _client()
    expected = {
        "modelId": bedrock_helper.MODEL_ID,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": 512},
    }
    with Stubber(client) as stub:
        stub.add_response("converse", _converse_response([{"text": "ok"}]), expected)
        assert bedrock_helper.ask_model(client, content, max_tokens=512) == "ok"
        stub.assert_no_pending_responses()


@_REQUESTS
def test_requests_pass_the_stack_guardrail(bedrock_helper, content, monkeypatch):
    """With the guardrail Terraform gives the webhook, each request carries it."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")
    client = _client()
    expected = {
        "modelId": bedrock_helper.MODEL_ID,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": 512},
        "guardrailConfig": {"guardrailIdentifier": "gr-test", "guardrailVersion": "3"},
    }
    with Stubber(client) as stub:
        stub.add_response("converse", _converse_response([{"text": "ok"}]), expected)
        assert bedrock_helper.ask_model(client, content, max_tokens=512) == "ok"
        stub.assert_no_pending_responses()


def test_guardrail_without_a_version_uses_its_draft(bedrock_helper, monkeypatch):
    """The agent's fallback (``AgentConfig.guardrail_config``), so the webhook
    applies the guardrail the agent applies."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "")
    assert bedrock_helper.guardrail_config() == {
        "guardrailIdentifier": "gr-test",
        "guardrailVersion": "DRAFT",
    }


def test_an_intervention_through_the_guardrail_raises(bedrock_helper, monkeypatch):
    """A mask stops with ``guardrail_intervened`` as a block does, so a masked
    description is dropped too, and the webhook falls back to
    ``[User sent an image]`` (or a document's file name) and any caption."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")
    content = [{"text": "Describe this image."}]
    client = _client()
    masked = _converse_response(
        [{"text": "A driver's license numbered {DRIVER_ID}."}], "guardrail_intervened"
    )
    with Stubber(client) as stub:
        stub.add_response(
            "converse",
            masked,
            {
                "modelId": bedrock_helper.MODEL_ID,
                "messages": [{"role": "user", "content": content}],
                "inferenceConfig": {"maxTokens": 512},
                "guardrailConfig": {
                    "guardrailIdentifier": "gr-test",
                    "guardrailVersion": "3",
                },
            },
        )
        with pytest.raises(bedrock_helper.NoTextResponse):
            bedrock_helper.ask_model(client, content, max_tokens=512)


def test_reads_generated_text_from_citations_content(bedrock_helper):
    """With document citations on, the generated text arrives inside
    citationsContent, not as a top-level text block."""
    response = _converse_response(
        [
            {
                "citationsContent": {
                    "content": [{"text": "Invoice total: 42."}],
                    "citations": [{"title": "document"}],
                }
            },
            {"text": " Due Friday."},
        ]
    )
    assert bedrock_helper.response_text(response) == "Invoice total: 42. Due Friday."


def test_all_cited_response_is_not_an_error(bedrock_helper):
    response = _converse_response(
        [
            {
                "citationsContent": {
                    "content": [{"text": "Only cited text."}],
                    "citations": [],
                }
            }
        ]
    )
    assert bedrock_helper.response_text(response) == "Only cited text."
