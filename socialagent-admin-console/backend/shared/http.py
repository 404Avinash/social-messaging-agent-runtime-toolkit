# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared HTTP response helpers for legacy handle_*(params, body) functions.

The handlers return this {statusCode, headers, body} shape; routes/ adapt it
to a Powertools Response via shared.resolver.from_legacy().
"""

from __future__ import annotations

import json

from aws_lambda_powertools import Logger

from shared.config import CORS_ALLOW_ORIGIN
from shared.resolver import app

logger = Logger(service="admin-console-backend", child=True)

# Match the resolver's CORS origin (first configured origin, or "*" if unset).
# These legacy responses are normally re-wrapped by resolver.from_legacy (which
# applies the resolver's CORSConfig), so this header is a consistency fallback —
# avoid emitting a wildcard that contradicts the configured origin.
_CORS_ORIGIN = next(
    (o.strip() for o in CORS_ALLOW_ORIGIN.split(",") if o.strip()),
    "*",
)


def json_response(status: int, body: dict | list) -> dict:
    """Build a legacy Lambda-proxy JSON response."""
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _CORS_ORIGIN,
        },
        "body": json.dumps(body, default=str),
    }


def request_id() -> str:
    """The API Gateway request id, or "" outside a request.

    handler.py logs it as ``correlation_id`` on every line, so it is what finds a
    failure in the backend's log group.
    """
    try:
        return app.current_event.request_context.request_id or ""
    except Exception:
        return ""


def logged_failure(message: str) -> str:
    """Log the exception being handled; return ``message`` plus the request id.

    Call from an ``except`` block. The exception's own text stays in the log:
    boto3 errors name ARNs, the account id and the backend's IAM role, which the
    browser has no use for.
    """
    rid = request_id()
    logger.exception(message, extra={"request_id": rid})
    return f"{message} (request id {rid})" if rid else message


def error_response(status: int, message: str, **extra) -> dict:
    """A JSON error for the exception being handled, without its text.

    ``extra`` adds fields to the body, as the handlers' own error dicts did.
    """
    body = {"error": logged_failure(message), **extra}
    rid = request_id()
    if rid:
        body["requestId"] = rid
    return json_response(status, body)


def failure_response(status: int, message: str, **extra) -> dict:
    """A JSON error shaped like ``error_response``'s, for a failure the
    caller has already logged and that has no exception being handled.

    ``error_response`` logs the exception being handled, so outside an
    ``except`` block it would log an empty traceback. The request id still
    goes in the body: it finds the failure's own log lines.
    """
    rid = request_id()
    body = {"error": f"{message} (request id {rid})" if rid else message, **extra}
    if rid:
        body["requestId"] = rid
    return json_response(status, body)
