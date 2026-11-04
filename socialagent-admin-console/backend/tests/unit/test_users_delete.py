# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DELETE /api/users/<username> signs the user out everywhere before deleting.

API Gateway's JWT authorizer checks only signature and expiry, so deleting a
console user left their issued tokens accepted until they expired. Global
sign-out revokes the refresh and access tokens at Cognito first.
"""

import json
from unittest.mock import MagicMock

import pytest

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.


@pytest.fixture
def cognito(monkeypatch):
    from routes import users

    client = MagicMock()
    monkeypatch.setattr(users, "COGNITO_USER_POOL_ID", "us-east-1_pool")
    monkeypatch.setattr(users.boto3, "client", lambda *a, **k: client)
    return client


def _delete(username="ana@example.com"):
    from routes import users

    resp = users.handle_delete_user({"username": username}, {})
    return resp["statusCode"], json.loads(resp["body"])


def test_signs_out_before_deleting(cognito):
    calls = []
    cognito.admin_user_global_sign_out.side_effect = lambda **kw: calls.append("sign_out")
    cognito.admin_delete_user.side_effect = lambda **kw: calls.append("delete")

    status, body = _delete()

    assert status == 200
    assert calls == ["sign_out", "delete"]
    assert body == {"deleted": True, "username": "ana@example.com", "signedOut": True}
    cognito.admin_user_global_sign_out.assert_called_once_with(
        UserPoolId="us-east-1_pool", Username="ana@example.com"
    )


def test_a_failed_sign_out_still_deletes_and_says_so(cognito):
    """Deletion is the stronger revocation; a sign-out failure must not block it."""
    cognito.admin_user_global_sign_out.side_effect = Exception("AccessDenied")

    status, body = _delete()

    assert status == 200
    cognito.admin_delete_user.assert_called_once()
    assert body["signedOut"] is False


def test_deleting_drops_the_users_cached_groups(cognito, monkeypatch):
    from routes import users

    forgotten = []
    monkeypatch.setattr(users, "forget_user", forgotten.append)
    _delete("ana@example.com")

    assert forgotten == ["ana@example.com"]


def test_failed_delete_is_a_500(cognito):
    cognito.admin_delete_user.side_effect = Exception("boom")

    status, body = _delete()

    assert status == 500
    assert body.get("deleted") is not True
