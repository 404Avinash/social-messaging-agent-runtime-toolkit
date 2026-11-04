# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every route carries @require_group, applied below the route decorator.

``require_group`` (shared/auth.py) is the backend's only authorization layer:
API Gateway's JWT authorizer proves who the caller is, and nothing else decides
what they may do. A route without it is open to any signed-in user, including
one in no group at all. So these tests enumerate the routes instead of trusting
each module to remember.

Decorator order matters as much as presence. Powertools registers whatever
function the route decorator receives, so with ``@require_group`` above
``@app.get`` the registered handler is the bare function and the guard only
wraps the module attribute, which no request reaches.

``EXPECTED_POLICY`` pins the level of every route. Adding a route, or moving one
between groups, means editing it here and in the roles table in
docs/admin-console.md.
"""

import ast
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[2]

EXPECTED_POLICY = {
    # viewer: the Stacks page
    ("GET", "/api/stacks"): "viewer",
    ("GET", "/api/stacks/<id>"): "viewer",
    ("GET", "/api/stacks/<id>/metrics"): "viewer",
    ("GET", "/api/stacks/<id>/deploys"): "viewer",
    # operator: everything else read, plus the non-destructive mutations
    ("GET", "/api/stacks/<id>/errors"): "operator",
    ("GET", "/api/stacks/<id>/conversations"): "operator",
    ("GET", "/api/stacks/<id>/conversations/<phone_hash>/messages"): "operator",
    ("GET", "/api/stacks/<id>/events"): "operator",
    ("GET", "/api/stacks/<id>/phone-numbers"): "operator",
    ("GET", "/api/stacks/<id>/state"): "operator",
    ("GET", "/api/stacks/<id>/templates"): "operator",
    ("GET", "/api/stacks/<id>/templates/<template_name>"): "operator",
    ("GET", "/api/phone-numbers"): "operator",
    ("GET", "/api/schedules"): "operator",
    ("GET", "/api/settings"): "operator",
    ("GET", "/api/state"): "operator",
    ("GET", "/api/templates/health"): "operator",
    ("GET", "/api/users"): "operator",
    ("PUT", "/api/logs"): "operator",
    ("PUT", "/api/phone-numbers/bind"): "operator",
    ("PUT", "/api/phone-numbers/unbind"): "operator",
    ("PUT", "/api/stacks/<id>/config"): "operator",
    ("PUT", "/api/stacks/<id>/restart-lambda/<lambda_name>"): "operator",
    ("PUT", "/api/stacks/<id>/restart-runtime"): "operator",
    ("PUT", "/api/stacks/<id>/send-message"): "operator",
    ("PUT", "/api/stacks/<id>/trigger-event"): "operator",
    # admin: destructive, user management, console settings
    ("DELETE", "/api/schedules/<schedule_name>"): "admin",
    ("DELETE", "/api/stacks/<id>/conversations"): "admin",
    ("DELETE", "/api/stacks/<id>/conversations/<phone_hash>"): "admin",
    ("DELETE", "/api/users/<username>"): "admin",
    ("POST", "/api/stacks/<id>/state/delete"): "admin",
    ("PUT", "/api/settings"): "admin",
    ("PUT", "/api/stacks/<id>/nuclear-reset"): "admin",
    ("PUT", "/api/stacks/<id>/update-sns-destination"): "admin",
    ("PUT", "/api/users"): "admin",
}

# The group one level below each requirement; "" is a caller in no group.
_BELOW = {"viewer": "", "operator": "viewer", "admin": "operator"}

_ROUTE_METHODS = {"get", "put", "post", "delete", "patch", "head", "route"}


def _registered_routes():
    """Every route on the shared resolver, after handler.py has imported them all."""
    import handler  # noqa: F401  (route modules register on import)
    from shared.resolver import app

    # Private on Powertools' resolver, but a rename fails the non-empty assert
    # below rather than passing vacuously.
    routes = list(app._static_routes) + list(app._dynamic_routes)
    assert routes, "no routes found on the resolver; has Powertools renamed its route lists?"
    return routes


def test_every_registered_route_is_guarded():
    from shared.auth import GROUP_LEVELS

    unguarded = [
        f"{r.method} {r.path} -> {r.func.__name__}"
        for r in _registered_routes()
        if getattr(r.func, "required_group", None) not in GROUP_LEVELS
    ]
    assert not unguarded, (
        "routes registered without @require_group (missing, or above the route "
        f"decorator so the bare function was registered): {unguarded}"
    )


def test_route_policy_is_pinned():
    actual = {(r.method, r.path): getattr(r.func, "required_group", None) for r in _registered_routes()}
    assert actual == EXPECTED_POLICY, (
        "a route was added, removed, or changed group; update EXPECTED_POLICY and "
        "the roles table in docs/admin-console.md together"
    )


def test_every_delete_route_requires_admin():
    loose = [k for k, group in EXPECTED_POLICY.items() if k[0] == "DELETE" and group != "admin"]
    assert not loose


def _route_decorator(node: ast.expr) -> bool:
    """True for ``@app.<method>(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "app"
        and node.func.attr in _ROUTE_METHODS
    )


def _group_decorator(node: ast.expr) -> str | None:
    """The literal group of ``@require_group("<group>")``, else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "require_group"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _source_routes():
    """(file:line, function, decorator list) for every route in the backend source."""
    files = sorted((BACKEND / "routes").glob("*.py")) + sorted((BACKEND / "shared").glob("*.py"))
    files.append(BACKEND / "handler.py")
    found = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _route_decorator(d) for d in node.decorator_list
            ):
                found.append((f"{path.name}:{node.lineno}", node.name, node.decorator_list))
    return found


def test_source_decorators_are_route_then_require_group():
    """Read the source too: the route decorator first, require_group directly under it."""
    from shared.auth import GROUP_LEVELS

    bad = []
    for where, name, decorators in _source_routes():
        shape_ok = (
            len(decorators) == 2
            and _route_decorator(decorators[0])
            and _group_decorator(decorators[1]) in GROUP_LEVELS
        )
        if not shape_ok:
            bad.append(f"{where} {name}")
    assert not bad, (
        "each route must be exactly @app.<method>(...) then @require_group(\"<group>\"): "
        f"{bad}"
    )


def test_source_and_resolver_agree_on_route_count():
    """Guards the AST walk itself: a route it cannot see is a route it cannot check."""
    assert len(_source_routes()) == len(_registered_routes())


def _event(method: str, path: str, groups: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {},
        "body": "{}",
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "test-api",
            "domainName": "test.execute-api.us-east-1.amazonaws.com",
            "domainPrefix": "test",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test",
            },
            "authorizer": {"jwt": {"claims": {"sub": "test-user", "cognito:groups": groups}}},
            "requestId": "test-req-id",
            "routeKey": f"{method} {path}",
            "stage": "$default",
            "time": "08/May/2026:12:00:00 +0000",
            "timeEpoch": 1746700800000,
        },
        "isBase64Encoded": False,
    }


@pytest.mark.parametrize("method,template", sorted(EXPECTED_POLICY))
def test_caller_one_level_short_is_refused(method, template, monkeypatch):
    """Through the real resolver: one group short of the route's level gets a 403."""
    from moto import mock_aws

    import shared.auth as auth

    # Authorize from the claim alone, so the test controls the caller's groups.
    monkeypatch.setattr(auth, "LIVE_GROUP_CHECK", False)
    required = EXPECTED_POLICY[(method, template)]
    path = re.sub(r"<(\w+)>", r"\1-x", template)
    ctx = MagicMock(function_name="admin-api", memory_limit_in_mb=256, aws_request_id="r")
    ctx.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:admin-api"

    # mock_aws keeps an unguarded route from reaching real AWS if this ever fails.
    with mock_aws():
        from handler import handler

        resp = handler(_event(method, path, _BELOW[required]), ctx)

    assert resp["statusCode"] == 403, f"{method} {path} let a '{_BELOW[required]}' caller through"
    assert json.loads(resp["body"])["requiredGroup"] == required
