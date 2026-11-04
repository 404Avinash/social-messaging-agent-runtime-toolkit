# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared APIGatewayHttpResolver instance + legacy response adapter.

Separated from handler.py to avoid circular imports: route modules import
`app` from here (decorators register on it), and handler.py imports the
route modules for side-effect registration.

The `from_legacy()` helper adapts the handle_*(params, body) style
(returning _json(status, body_dict)) to Powertools' Response, so handlers can be
decorated without changing their bodies.
"""

from __future__ import annotations

import json

from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig

from shared.config import CORS_ALLOW_ORIGIN

# Restrict CORS to the known console origin(s) instead of "*". Set CORS_ALLOW_ORIGIN
# to the CloudFront/custom-domain URL of the admin console (comma-separated for
# multiple). Falls back to "*" only when unset, so existing deploys keep working
# until the env var is wired — tighten it in the API/runtime env block.
_origins = [
    o.strip() for o in CORS_ALLOW_ORIGIN.split(",") if o.strip()
]
cors_config = CORSConfig(
    allow_origin=_origins[0] if _origins else "*",
    extra_origins=_origins[1:] if len(_origins) > 1 else None,
    max_age=3600,
)
# No global authorization middleware: every route carries @require_group from
# shared/auth.py, and tests/unit/test_route_authorization.py fails if one does not.
app = APIGatewayHttpResolver(cors=cors_config)


def from_legacy(legacy_resp: dict) -> Response:
    """Convert a legacy _json(status, body)-style Lambda response dict to a Response.

    Legacy shape:
        {"statusCode": 200, "headers": {...}, "body": "<json string>"}

    Powertools Response expects body as string (we already have it).
    """
    return Response(
        status_code=legacy_resp.get("statusCode", 200),
        content_type="application/json",
        body=legacy_resp.get("body", "{}"),
    )


def current_body() -> dict:
    """Return the current request's parsed JSON body, or {} if empty/invalid.

    Equivalent to what handler.py's top-level handler() used to do before
    dispatching to a handler.
    """
    try:
        return app.current_event.json_body or {}
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}


def query_params() -> dict:
    """Return the current request's query string parameters, or {} if none.

    Mirrors current_body() for query strings so routes can thread params like
    ?cursor=<token>&page_size=25 into the legacy handle_*(params, body) calls.
    """
    try:
        return app.current_event.query_string_parameters or {}
    except (TypeError, AttributeError):
        return {}
