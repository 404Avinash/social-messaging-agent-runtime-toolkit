# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Error responses name what failed, never the exception's own text.

The routes used to return ``f"Restart failed: {e}"`` and the like. boto3
exception text carries ARNs, the account id and the backend's IAM role name,
none of which the browser needs. The exception now goes to the log, and the
response carries the request id that finds it there.
"""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

BACKEND = Path(__file__).resolve().parents[2]
INTERNAL_DETAIL = "arn:aws:iam::111122223333:role/admin-console-backend"


def _denied() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"User: {INTERNAL_DETAIL} is not authorized"}},
        "UpdateFunctionConfiguration",
    )


def test_error_response_keeps_status_and_hides_the_exception(monkeypatch, caplog):
    from shared import http

    monkeypatch.setattr(http, "request_id", lambda: "req-123")
    try:
        raise _denied()
    except ClientError:
        resp = http.error_response(500, "Restart failed", stack="bot-dev")

    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert body == {"error": "Restart failed (request id req-123)", "stack": "bot-dev", "requestId": "req-123"}
    # The detail is not lost: it is logged with the traceback.
    record = next(r for r in caplog.records if r.getMessage() == "Restart failed")
    assert record.exc_info and INTERNAL_DETAIL in str(record.exc_info[1])


def test_outside_a_request_there_is_no_request_id_to_show(monkeypatch):
    from shared import http

    monkeypatch.setattr(http, "request_id", lambda: "")
    try:
        raise RuntimeError(INTERNAL_DETAIL)
    except RuntimeError:
        body = json.loads(http.error_response(400, "Could not save settings")["body"])

    assert body == {"error": "Could not save settings"}


def test_a_failure_already_logged_logs_no_empty_traceback(monkeypatch, caplog):
    """A log route that read no log group answers 502 outside an except
    block: error_response would log the exception being handled, which
    there is none of."""
    import logging

    from shared import http

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(http, "request_id", lambda: "req-123")
    body = json.loads(http.failure_response(502, "Could not read any log group", unreadable=[])["body"])
    assert body == {"error": "Could not read any log group (request id req-123)", "unreadable": [], "requestId": "req-123"}

    monkeypatch.setattr(http, "request_id", lambda: "")
    assert json.loads(http.failure_response(502, "Could not read any log group")["body"]) == {
        "error": "Could not read any log group"
    }
    assert caplog.records == []


def test_a_route_failure_does_not_echo_the_arn():
    """End to end through one handler: the AccessDenied text stays server-side."""
    from routes import ops

    lam = MagicMock()
    lam.get_function_configuration.side_effect = _denied()
    original_client, original_registry = ops.lambda_client, ops._get_registry
    ops.lambda_client = lam
    ops._get_registry = lambda _id: {"outputs": {"webhook_lambda": "dev-bot-webhook"}}
    try:
        resp = ops.handle_restart_lambda({"id": "bot-dev", "lambda_name": "webhook"}, {})
    finally:
        ops.lambda_client, ops._get_registry = original_client, original_registry

    assert resp["statusCode"] == 500
    assert INTERNAL_DETAIL not in resp["body"]
    assert json.loads(resp["body"])["error"].startswith("Restart failed")


def _interpolated_exceptions(path: Path) -> list[str]:
    """f-strings in ``except ... as <name>`` bodies that interpolate <name>."""
    found = []
    for handler in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if isinstance(node, ast.FormattedValue) and any(
                isinstance(n, ast.Name) and n.id == handler.name for n in ast.walk(node.value)
            ):
                found.append(f"{path.name}:{node.lineno}")
    return found


@pytest.mark.parametrize("path", sorted((BACKEND / "routes").glob("*.py")), ids=lambda p: p.name)
def test_no_route_formats_an_exception_into_a_response(path):
    assert not _interpolated_exceptions(path), (
        "use shared.http.error_response / logged_failure instead of formatting the "
        "exception into the response"
    )
