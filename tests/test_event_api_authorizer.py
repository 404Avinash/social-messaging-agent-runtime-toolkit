# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The event API's Lambda authorizer (infra-terraform/modules/event-api/authorizer.py).

HTTP header names are case-insensitive, and a REST API REQUEST authorizer gets
them in the case the client sent. urllib, which the admin console's Trigger
Event uses, sends ``X-webhook-secret``; other clients send ``X-Webhook-Secret``.
The authorizer must accept the secret under any of them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

AUTHORIZER = (
    Path(__file__).resolve().parents[1]
    / "infra-terraform"
    / "modules"
    / "event-api"
    / "authorizer.py"
)
SECRET = "correct-horse-battery-staple"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:event-webhook"
METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/POST/events"


def _load(
    monkeypatch: pytest.MonkeyPatch, stored: dict[str, str] | None = None, **env: str
) -> ModuleType:
    """A fresh copy of the authorizer, reading *stored* (by default the
    ``webhook_secret``) from a stub client."""
    monkeypatch.setenv("SECRET_ARN", SECRET_ARN)
    monkeypatch.delenv("AUTH_HEADER_NAME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = MagicMock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {"webhook_secret": SECRET} if stored is None else stored
        )
    }
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)
    spec = importlib.util.spec_from_file_location("event_api_authorizer", AUTHORIZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _effect(authorizer: ModuleType, headers: dict[str, str] | None) -> str:
    result = authorizer.handler({"headers": headers, "methodArn": METHOD_ARN}, None)
    [statement] = result["policyDocument"]["Statement"]
    assert statement["Resource"] == METHOD_ARN
    return statement["Effect"]


@pytest.fixture
def authorizer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _load(monkeypatch)


@pytest.mark.parametrize(
    "header",
    ["x-webhook-secret", "X-webhook-secret", "X-Webhook-Secret", "X-WEBHOOK-SECRET"],
)
def test_secret_is_accepted_in_any_header_case(
    authorizer: ModuleType, header: str
) -> None:
    headers = {header: SECRET, "Content-Type": "application/json"}
    assert _effect(authorizer, headers) == "Allow"


@pytest.mark.parametrize("header", ["x-webhook-secret", "X-Webhook-Secret"])
def test_wrong_secret_is_denied(authorizer: ModuleType, header: str) -> None:
    assert _effect(authorizer, {header: SECRET + "x"}) == "Deny"


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"Content-Type": "application/json"},
        {"X-Webhook-Secret": ""},
        {"Authorization": SECRET},
    ],
    ids=[
        "no-headers",
        "empty",
        "header-missing",
        "header-empty",
        "secret-in-other-header",
    ],
)
def test_request_without_the_secret_header_is_denied(
    authorizer: ModuleType, headers: dict[str, str] | None
) -> None:
    assert _effect(authorizer, headers) == "Deny"


@pytest.mark.parametrize(
    "headers",
    [None, {}, {"X-Webhook-Secret": ""}],
    ids=["no-headers", "header-missing", "header-empty"],
)
def test_a_missing_stored_secret_denies_a_request_without_one(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str] | None
) -> None:
    # A secret with no webhook_secret key reads as "", and compare_digest("", "")
    # is True, so only the empty-token check keeps this a Deny.
    authorizer = _load(monkeypatch, stored={})

    assert _effect(authorizer, headers) == "Deny"


def test_configured_header_name_is_matched_in_any_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = _load(monkeypatch, AUTH_HEADER_NAME="X-Partner-Token")

    assert _effect(authorizer, {"x-partner-token": SECRET}) == "Allow"
    assert _effect(authorizer, {"X-PARTNER-TOKEN": SECRET}) == "Allow"
    assert _effect(authorizer, {"X-Webhook-Secret": SECRET}) == "Deny"
