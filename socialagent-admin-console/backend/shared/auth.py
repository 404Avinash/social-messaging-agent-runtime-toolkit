# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authorization — deny-by-default with Cognito group enforcement.

``require_group`` is the backend's only authorization layer. API Gateway's JWT
authorizer proves who the caller is; nothing else decides what they may do, so
every route carries this decorator (tests/unit/test_route_authorization.py
enumerates the registered routes and fails on one that does not).

Group membership is resolved from one of two sources:

1. Live Cognito lookup (``AdminGetUser`` + ``AdminListGroupsForUser``) —
   authoritative and current. Bounded by a short in-memory TTL cache so a
   burst of requests (a single page load fires ~10) collapses to one lookup
   per warm container per TTL window. This is the default and closes the JWT
   stale-claim window: a revoked user loses access within the cache TTL
   (seconds) instead of waiting up to an hour for their ID token to expire.
   A deleted user (``UserNotFoundException``) or a disabled one has no
   groups, and that answer is cached like any other.

2. JWT ``cognito:groups`` claim — used only when Cognito could not answer
   (throttling, a Cognito 5xx, a network failure) and when live checks are
   disabled via ``AUTH_LIVE_GROUP_CHECK=false``. The claim is a signed,
   validated assertion, so the fallback is bounded-stale. It is never used
   when Cognito did answer: falling back on *any* error let a deleted user's
   still-valid token keep its groups, because the lookup for them fails too.
   Any other error (an IAM denial, a wrong pool id) denies the request.

Groups (hierarchical — higher includes lower):
    admin    → full control (destructive ops, user management, settings)
    operator → read + mutate stacks (restart, trigger, send, bind)
    viewer   → read-only access
"""
from __future__ import annotations

import functools
import json
import os
from typing import Callable

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Response
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from shared import clients
from shared.cache import TTLCache
from shared.resolver import app

logger = Logger(service="admin-console-auth")

# Hierarchy levels — higher number = more permissions
GROUP_LEVELS: dict[str, int] = {
    "admin": 3,
    "operator": 2,
    "viewer": 1,
}

REGION = os.environ.get("AWS_REGION", "us-east-1")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")

# Live authoritative group lookup (default on). Set AUTH_LIVE_GROUP_CHECK=false
# to fall back to the JWT claim only (skips the Cognito call + IAM requirement).
LIVE_GROUP_CHECK = os.environ.get("AUTH_LIVE_GROUP_CHECK", "true").strip().lower() == "true"

# Bounds staleness of the live lookup and absorbs per-page-load request bursts.
# A malformed env value must never brick Lambda init — fall back to 60s.
try:
    _ttl_seconds = int(os.environ.get("AUTH_GROUP_CACHE_TTL", "60"))
except (TypeError, ValueError):
    _ttl_seconds = 60
_GROUP_CACHE_TTL = max(_ttl_seconds, 1)
_group_cache = TTLCache(ttl_seconds=_GROUP_CACHE_TTL)

# Cognito could not answer right now, so the signed claim may stand in. Any
# 5xx counts too, matched by status for codes not listed here.
_TRANSIENT_CODES = frozenset({
    "TooManyRequestsException", "ThrottlingException", "Throttling",
    "RequestLimitExceeded", "InternalErrorException", "ServiceUnavailable",
})


def _claims() -> dict:
    """Return the current request's verified JWT claims, or {}."""
    try:
        return app.current_event.request_context.authorizer.jwt_claim or {}
    except (AttributeError, TypeError):
        return {}


def _normalize(groups) -> list[str]:
    """Lowercase + strip a list or delimited string of group names.

    API Gateway HTTP API serializes ``cognito:groups`` as a bracketed string like
    ``"[admin operator]"``, not a clean space-separated list. Parsing only
    whitespace turned ``"[admin]"`` into the single group ``"[admin]"``,
    matching nothing, so the JWT-claim fallback (used whenever the live Cognito
    lookup is unavailable) fail-closed every user out of every route. Strip the
    brackets and commas too.
    """
    if isinstance(groups, list):
        return [g.strip().lower() for g in groups if str(g).strip()]
    cleaned = str(groups).strip("[]").replace(",", " ")
    return [g.strip().lower() for g in cleaned.split() if g.strip()]


def _claim_groups() -> list[str]:
    """Groups asserted by the signed JWT (the fallback source). May be stale."""
    raw = _claims().get("cognito:groups", "")
    return _normalize(raw) if raw else []


def _current_username() -> str:
    """Stable identifier for the caller, preferred for the Cognito lookup."""
    claims = _claims()
    return claims.get("cognito:username") or claims.get("sub") or ""


def _error_code(exc: Exception) -> str:
    return exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""


def _is_transient(exc: Exception) -> bool:
    """Throttling, a Cognito 5xx, or a network failure: no answer about the user."""
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return True
    if not isinstance(exc, ClientError):
        return False
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
    return _error_code(exc) in _TRANSIENT_CODES or status >= 500


def forget_user(username: str) -> None:
    """Drop a cached lookup, e.g. right after the user is deleted.

    Only this container's cache; the others expire within the TTL.
    """
    _group_cache.delete(username)


def _live_groups(username: str) -> list[str] | None:
    """Authoritative groups from Cognito (the primary source), cached for TTL seconds.

    Returns None only when Cognito could not answer (see ``_is_transient``), so
    the caller can fall back to the JWT claim. A user Cognito does not have, or
    has disabled, gets ``[]``, cached. Any other failure also gets ``[]`` but is
    not cached, so fixing the cause (an IAM policy, the pool id) takes effect on
    the next request.
    """
    if not USER_POOL_ID or not username:
        return None

    cached = _group_cache.get(username)
    if cached is not None:
        return cached

    try:
        idp = clients.client("cognito-idp", REGION)
        # A disabled user's tokens still pass API Gateway's JWT check until
        # they expire, and Cognito still lists their groups, so check the
        # account itself first.
        if idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=username).get("Enabled") is False:
            logger.info("live_group_lookup_user_disabled")
            _group_cache.set(username, [])
            return []
        groups: list[str] = []
        next_token = None
        while True:
            kwargs = {"UserPoolId": USER_POOL_ID, "Username": username}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = idp.admin_list_groups_for_user(**kwargs)
            groups.extend(
                g["GroupName"].strip().lower()
                for g in resp.get("Groups", [])
                if g.get("GroupName")
            )
            next_token = resp.get("NextToken")
            if not next_token:
                break
        _group_cache.set(username, groups)
        return groups
    except Exception as exc:
        if _error_code(exc) == "UserNotFoundException":
            logger.info("live_group_lookup_user_not_found")
            _group_cache.set(username, [])
            return []
        if _is_transient(exc):
            logger.warning("live_group_lookup_failed_falling_back_to_claim")
            return None
        logger.exception("live_group_lookup_failed_denying")
        return []


def get_user_groups() -> list[str]:
    """Resolve the caller's groups — live (authoritative) with claim fallback."""
    if LIVE_GROUP_CHECK:
        live = _live_groups(_current_username())
        if live is not None:
            return live
        # Cognito could not answer: the signed claim is bounded-stale, not open.
    return _claim_groups()


def has_permission(user_groups: list[str], required_group: str) -> bool:
    """Check if any user group meets or exceeds the required level."""
    required_level = GROUP_LEVELS.get(required_group, 999)
    return any(GROUP_LEVELS.get(g, 0) >= required_level for g in user_groups)


def require_group(required_group: str) -> Callable:
    """Decorator enforcing group-based authorization on a route handler.

    Apply AFTER @app.get/put/post/delete (closer to the function definition).
    Powertools registers whatever function the route decorator receives, so
    with the order reversed the registered handler is the bare function and
    the check never runs. Deny-by-default: no groups or insufficient level
    returns 403.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            user_groups = get_user_groups()
            if not has_permission(user_groups, required_group):
                return Response(
                    status_code=403,
                    content_type="application/json",
                    body=json.dumps({
                        "error": "Forbidden",
                        "message": f"Requires '{required_group}' group membership",
                        "requiredGroup": required_group,
                    }),
                )
            return func(*args, **kwargs)

        # Lets the route-authorization test read the level off a registered route.
        wrapper.required_group = required_group
        return wrapper

    return decorator
