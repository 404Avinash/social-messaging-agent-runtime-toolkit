# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the upload_media notification path (handler + agent-side helper).

Covers:
- Notification handler routes the ``upload_media`` operation to
  ``provider.upload_media_from_s3`` and returns ``{"media_id": ...}``.
- ``socialagent_runtime.notify.upload_media`` returns a stub in dev mode without
  invoking the Lambda client.
- ``socialagent_runtime.notify.upload_media`` invokes the notification Lambda with
  ``InvocationType="RequestResponse"`` in production mode and parses the
  returned media_id.

All AWS calls are mocked — these run fully offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.handlers.notification import make_notification_handler

# Make the agent-side notify module importable the same way the rest of the
# suite does (packages/socialagent-runtime is a dash-named dir wired in via sys.path).
_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import notify as notify_mod  # noqa: E402


# ── notification handler routing ───────────────────────────────────────────────


def test_handler_routes_upload_media() -> None:
    """upload_media operation calls provider.upload_media_from_s3 and returns media_id."""
    provider = MagicMock()
    provider.upload_media_from_s3 = AsyncMock(return_value="wamid.TEST123")
    memory = MagicMock()
    memory.log_assistant_message = AsyncMock()

    handler = make_notification_handler(provider, memory)
    result = handler({"operation": "upload_media", "data": {"bucket": "b", "key": "k"}})

    provider.upload_media_from_s3.assert_awaited_once_with("b", "k")
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["media_id"] == "wamid.TEST123"


# ── agent-side upload_media: dev mode ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_media_dev_mode_returns_stub() -> None:
    """In dev mode, upload_media returns the stub id without invoking the Lambda."""
    # The dev sentinel is what enables dev mode (see AgentConfig.is_dev_mode);
    # an empty ARN is a broken deployment and now refuses to build a config.
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
    )
    mock_client = MagicMock()
    try:
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            result = await notify_mod.upload_media("bk", "key")
        assert result == "dev-media-id"
        mock_client.invoke.assert_not_called()
    finally:
        object.__setattr__(
            notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


# ── agent-side upload_media: production mode ───────────────────────────────────


@pytest.mark.asyncio
async def test_upload_media_production_invokes_lambda() -> None:
    """In production mode, upload_media invokes the Lambda (RequestResponse) and parses media_id."""
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "arn:aws:lambda:::function:notify"
    )
    try:
        # Mimic the notification handler's RequestResponse payload: an outer
        # envelope whose "body" is itself a JSON string.
        inner_body = json.dumps({"media_id": "wamid.PROD"})
        outer_payload = json.dumps({"statusCode": 200, "body": inner_body}).encode()
        payload_stream = MagicMock()
        payload_stream.read.return_value = outer_payload

        mock_client = MagicMock()
        mock_client.invoke.return_value = {"Payload": payload_stream}

        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            result = await notify_mod.upload_media("bk", "key")

        assert result == "wamid.PROD"
        mock_client.invoke.assert_called_once()
        call_kwargs = mock_client.invoke.call_args[1]
        assert call_kwargs["InvocationType"] == "RequestResponse"
        assert call_kwargs["FunctionName"] == "arn:aws:lambda:::function:notify"
    finally:
        object.__setattr__(
            notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


# ── agent-side upload_media: defensive parsing (crash-safety) ──────────────────


def _prod_arn() -> None:
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "arn:aws:lambda:::function:notify"
    )


def _reset_arn() -> None:
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
    )


def _client_returning(
    payload_bytes: bytes, *, function_error: str | None = None
) -> MagicMock:
    """Build a mock Lambda client whose invoke() returns the given payload bytes."""
    stream = MagicMock()
    stream.read.return_value = payload_bytes
    resp: dict = {"Payload": stream}
    if function_error:
        resp["FunctionError"] = function_error
    client = MagicMock()
    client.invoke.return_value = resp
    return client


@pytest.mark.asyncio
async def test_upload_media_missing_media_id_returns_empty() -> None:
    """A well-formed 200 response without a media_id returns '' (no KeyError)."""
    _prod_arn()
    try:
        # e.g. provider returned an empty id → handler body has no media_id
        outer = json.dumps({"statusCode": 200, "body": json.dumps({})}).encode()
        client = _client_returning(outer)
        with patch.object(notify_mod, "_lambda_client", return_value=client):
            result = await notify_mod.upload_media("bk", "key")
        assert result == ""
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_upload_media_error_envelope_returns_empty() -> None:
    """A non-2xx error envelope with a plain-string body returns '' (no JSONDecodeError)."""
    _prod_arn()
    try:
        # The handler's unknown-operation path returns a plain-string body.
        outer = json.dumps(
            {"statusCode": 400, "body": "unknown operation: upload_media"}
        ).encode()
        client = _client_returning(outer)
        with patch.object(notify_mod, "_lambda_client", return_value=client):
            result = await notify_mod.upload_media("bk", "key")
        assert result == ""
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_upload_media_unparseable_payload_returns_empty() -> None:
    """A payload that is not valid JSON returns '' rather than raising."""
    _prod_arn()
    try:
        client = _client_returning(b"not json at all")
        with patch.object(notify_mod, "_lambda_client", return_value=client):
            result = await notify_mod.upload_media("bk", "key")
        assert result == ""
    finally:
        _reset_arn()


@pytest.mark.asyncio
async def test_upload_media_function_error_raises() -> None:
    """A Lambda FunctionError (handler raised) surfaces as a descriptive RuntimeError."""
    _prod_arn()
    try:
        traceback_payload = json.dumps(
            {
                "errorMessage": "boom",
                "errorType": "RuntimeError",
            }
        ).encode()
        client = _client_returning(traceback_payload, function_error="Unhandled")
        with patch.object(notify_mod, "_lambda_client", return_value=client):
            with pytest.raises(RuntimeError, match="notification Lambda error"):
                await notify_mod.upload_media("bk", "key")
    finally:
        _reset_arn()
