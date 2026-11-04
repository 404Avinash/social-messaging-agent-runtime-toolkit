# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for shared/auth.py — deny-by-default Cognito group authorization."""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "AdminListGroupsForUser",
    )


@pytest.fixture
def mock_app():
    """Patch the Powertools app to simulate JWT claims in requests."""
    with patch("shared.auth.app") as mock:
        yield mock


def _set_groups(mock_app, groups_value):
    """Configure mock to return the given cognito:groups claim value."""
    claims = {"cognito:groups": groups_value, "email": "test@example.com"}
    mock_app.current_event.request_context.authorizer.jwt_claim = claims


def _set_no_claims(mock_app):
    """Simulate a request with no JWT claims (edge case)."""
    mock_app.current_event.request_context.authorizer.jwt_claim = {}


def _set_no_authorizer(mock_app):
    """Simulate a request where authorizer context is missing entirely."""
    mock_app.current_event.request_context.authorizer = None


class TestGetUserGroups:
    def test_space_separated_string(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, "admin operator viewer")
        assert get_user_groups() == ["admin", "operator", "viewer"]

    def test_single_group_string(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, "viewer")
        assert get_user_groups() == ["viewer"]

    def test_list_input(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, ["Admin", "Operator"])
        assert get_user_groups() == ["admin", "operator"]

    def test_empty_string(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, "")
        assert get_user_groups() == []

    def test_no_claims(self, mock_app):
        from shared.auth import get_user_groups
        _set_no_claims(mock_app)
        assert get_user_groups() == []

    def test_no_authorizer(self, mock_app):
        from shared.auth import get_user_groups
        _set_no_authorizer(mock_app)
        assert get_user_groups() == []

    def test_none_value(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, None)
        assert get_user_groups() == []

    def test_bracketed_string(self, mock_app):
        # API Gateway HTTP API serializes the claim as "[admin operator]".
        # Parsing only whitespace kept the brackets, so the group matched nothing
        # and the claim fallback locked every user out.
        from shared.auth import get_user_groups
        _set_groups(mock_app, "[admin operator]")
        assert get_user_groups() == ["admin", "operator"]

    def test_bracketed_single_group(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, "[admin]")
        assert get_user_groups() == ["admin"]

    def test_bracketed_comma_separated(self, mock_app):
        from shared.auth import get_user_groups
        _set_groups(mock_app, "[admin, operator]")
        assert get_user_groups() == ["admin", "operator"]


class TestHasPermission:
    def test_admin_has_admin_permission(self):
        from shared.auth import has_permission
        assert has_permission(["admin"], "admin") is True

    def test_admin_has_operator_permission(self):
        from shared.auth import has_permission
        assert has_permission(["admin"], "operator") is True

    def test_admin_has_viewer_permission(self):
        from shared.auth import has_permission
        assert has_permission(["admin"], "viewer") is True

    def test_operator_has_operator_permission(self):
        from shared.auth import has_permission
        assert has_permission(["operator"], "operator") is True

    def test_operator_has_viewer_permission(self):
        from shared.auth import has_permission
        assert has_permission(["operator"], "viewer") is True

    def test_operator_lacks_admin_permission(self):
        from shared.auth import has_permission
        assert has_permission(["operator"], "admin") is False

    def test_viewer_has_viewer_permission(self):
        from shared.auth import has_permission
        assert has_permission(["viewer"], "viewer") is True

    def test_viewer_lacks_operator_permission(self):
        from shared.auth import has_permission
        assert has_permission(["viewer"], "operator") is False

    def test_viewer_lacks_admin_permission(self):
        from shared.auth import has_permission
        assert has_permission(["viewer"], "admin") is False

    def test_empty_groups_denied(self):
        from shared.auth import has_permission
        assert has_permission([], "viewer") is False

    def test_unknown_group_denied(self):
        from shared.auth import has_permission
        assert has_permission(["unknown"], "viewer") is False

    def test_multiple_groups_uses_highest(self):
        from shared.auth import has_permission
        assert has_permission(["viewer", "operator"], "operator") is True


class TestRequireGroupDecorator:
    def test_allows_authorized_request(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "admin")

        @require_group("viewer")
        def handler():
            return "ok"

        assert handler() == "ok"

    def test_denies_unauthorized_request(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "viewer")

        @require_group("admin")
        def handler():
            return "ok"

        resp = handler()
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["error"] == "Forbidden"
        assert body["requiredGroup"] == "admin"

    def test_denies_no_groups(self, mock_app):
        from shared.auth import require_group
        _set_no_claims(mock_app)

        @require_group("viewer")
        def handler():
            return "ok"

        resp = handler()
        assert resp.status_code == 403

    def test_preserves_function_args(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "operator")

        @require_group("operator")
        def handler(id: str, name: str):
            return f"{id}-{name}"

        assert handler("stack-1", "test") == "stack-1-test"

    def test_operator_can_access_operator_route(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "operator")

        @require_group("operator")
        def handler():
            return "ok"

        assert handler() == "ok"

    def test_operator_cannot_access_admin_route(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "operator")

        @require_group("admin")
        def handler():
            return "ok"

        resp = handler()
        assert resp.status_code == 403

    def test_case_insensitive_groups(self, mock_app):
        from shared.auth import require_group
        _set_groups(mock_app, "Admin")

        @require_group("admin")
        def handler():
            return "ok"

        assert handler() == "ok"


class TestLiveGroupLookup:
    """Live authoritative Cognito lookup with TTL cache + fail-safe fallback."""

    @pytest.fixture
    def live_auth(self, mock_app, monkeypatch):
        """Enable live checks with a fake Cognito client + fresh cache."""
        import shared.auth as auth
        from shared.cache import TTLCache

        monkeypatch.setattr(auth, "USER_POOL_ID", "us-east-1_pool")
        monkeypatch.setattr(auth, "LIVE_GROUP_CHECK", True)
        monkeypatch.setattr(auth, "_group_cache", TTLCache(ttl_seconds=60))

        idp = MagicMock()
        monkeypatch.setattr(auth.clients, "client", lambda *a, **k: idp)
        # Caller identity used as the Cognito Username
        mock_app.current_event.request_context.authorizer.jwt_claim = {
            "cognito:username": "user-123", "cognito:groups": "viewer",
        }
        return auth, idp

    def test_live_lookup_is_authoritative_over_claim(self, live_auth):
        # Claim says viewer, but Cognito says admin — live wins.
        auth, idp = live_auth
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "admin"}]}
        assert auth.get_user_groups() == ["admin"]

    def test_live_revocation_reflected(self, live_auth):
        # Claim still says viewer, but Cognito shows no groups — access removed.
        auth, idp = live_auth
        idp.admin_list_groups_for_user.return_value = {"Groups": []}
        assert auth.get_user_groups() == []

    def test_result_is_cached_within_ttl(self, live_auth):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "operator"}]}
        auth.get_user_groups()
        auth.get_user_groups()
        # Second call served from cache — only one Cognito call.
        assert idp.admin_list_groups_for_user.call_count == 1

    @pytest.mark.parametrize("error", [
        _client_error("TooManyRequestsException"),
        _client_error("InternalErrorException", status=500),
        _client_error("SomeNewServerFault", status=503),
        EndpointConnectionError(endpoint_url="https://cognito-idp.us-east-1.amazonaws.com"),
        ReadTimeoutError(endpoint_url="https://cognito-idp.us-east-1.amazonaws.com"),
    ])
    def test_falls_back_to_claim_when_cognito_cannot_answer(self, live_auth, error):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.side_effect = error
        # Claim (viewer) is honored as bounded-stale fail-safe.
        assert auth.get_user_groups() == ["viewer"]

    def test_deleted_user_gets_no_groups_not_their_claim(self, live_auth):
        """The claim still says viewer, but the user is gone: Cognito has answered."""
        auth, idp = live_auth
        idp.admin_get_user.side_effect = _client_error("UserNotFoundException")
        assert auth.get_user_groups() == []

    def test_deleted_user_answer_is_cached(self, live_auth):
        auth, idp = live_auth
        idp.admin_get_user.side_effect = _client_error("UserNotFoundException")
        auth.get_user_groups()
        auth.get_user_groups()
        assert idp.admin_get_user.call_count == 1

    def test_user_not_found_from_the_group_listing_counts_too(self, live_auth):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.side_effect = _client_error("UserNotFoundException")
        assert auth.get_user_groups() == []

    def test_disabled_user_gets_no_groups(self, live_auth):
        """Cognito keeps a disabled user's group memberships; access must not follow."""
        auth, idp = live_auth
        idp.admin_get_user.return_value = {"Enabled": False}
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "admin"}]}
        assert auth.get_user_groups() == []
        auth.get_user_groups()
        assert idp.admin_get_user.call_count == 1  # cached
        idp.admin_list_groups_for_user.assert_not_called()

    def test_enabled_user_proceeds_to_the_group_listing(self, live_auth):
        auth, idp = live_auth
        idp.admin_get_user.return_value = {"Enabled": True}
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "operator"}]}
        assert auth.get_user_groups() == ["operator"]

    @pytest.mark.parametrize("error", [
        _client_error("AccessDeniedException"),
        _client_error("ResourceNotFoundException"),
        RuntimeError("bug"),
    ])
    def test_other_errors_deny_without_trusting_the_claim(self, live_auth, error):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.side_effect = error
        assert auth.get_user_groups() == []

    def test_a_denial_from_an_error_is_not_cached(self, live_auth):
        """Fixing the cause (an IAM policy, say) must take effect on the next request."""
        auth, idp = live_auth
        idp.admin_list_groups_for_user.side_effect = [
            _client_error("AccessDeniedException"),
            {"Groups": [{"GroupName": "operator"}]},
        ]
        assert auth.get_user_groups() == []
        assert auth.get_user_groups() == ["operator"]

    def test_forget_user_drops_the_cached_lookup(self, live_auth):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "admin"}]}
        auth.get_user_groups()
        auth.forget_user("user-123")
        auth.get_user_groups()
        assert idp.admin_list_groups_for_user.call_count == 2

    def test_falls_back_to_claim_when_no_pool_id(self, mock_app, monkeypatch):
        import shared.auth as auth
        monkeypatch.setattr(auth, "USER_POOL_ID", "")
        monkeypatch.setattr(auth, "LIVE_GROUP_CHECK", True)
        _set_groups(mock_app, "operator")
        assert auth.get_user_groups() == ["operator"]

    def test_live_check_disabled_uses_claim(self, mock_app, monkeypatch):
        import shared.auth as auth
        monkeypatch.setattr(auth, "LIVE_GROUP_CHECK", False)
        _set_groups(mock_app, "admin")
        assert auth.get_user_groups() == ["admin"]

    def test_pagination_aggregates_all_groups(self, live_auth):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.side_effect = [
            {"Groups": [{"GroupName": "viewer"}], "NextToken": "t1"},
            {"Groups": [{"GroupName": "operator"}]},
        ]
        assert set(auth.get_user_groups()) == {"viewer", "operator"}

    def test_unknown_live_group_grants_nothing(self, live_auth):
        auth, idp = live_auth
        idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "random"}]}
        from shared.auth import has_permission
        assert has_permission(auth.get_user_groups(), "viewer") is False



class TestErrorsRouteGating:
    """`/errors` returns raw Lambda logs (end-user phone numbers, ARNs), so it
    must sit at operator, matching /logs and /conversations — not viewer."""

    def test_get_errors_requires_operator(self):
        import inspect
        from routes import stacks

        src = inspect.getsource(stacks.get_errors)
        assert '@require_group("operator")' in src or "require_group('operator')" in src
        assert '"viewer"' not in src and "'viewer'" not in src
