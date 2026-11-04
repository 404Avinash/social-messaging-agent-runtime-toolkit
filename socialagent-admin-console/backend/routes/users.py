# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Users routes: list, create, delete (Cognito operations)."""
from __future__ import annotations

import boto3
from aws_lambda_powertools import Logger

from shared.auth import forget_user, require_group
from shared.aws import REGION
from shared.config import COGNITO_USER_POOL_ID
from shared.http import error_response as _error, json_response as _json
from shared.resolver import app, current_body, from_legacy

logger = Logger(service="admin-console-backend", child=True)


def handle_create_user(_params, body):
    """Create a new Cognito admin user."""
    if not COGNITO_USER_POOL_ID:
        return _json(400, {"error": "COGNITO_USER_POOL_ID not configured"})
    email = body.get("email", "")
    if not email:
        return _json(400, {"error": "email is required"})
    try:
        cognito = boto3.client("cognito-idp", region_name=REGION)
        cognito.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],
        )
        return _json(200, {"created": True, "email": email})
    except Exception:
        return _error(500, "Create user failed")


def handle_delete_user(params, _body):
    """Sign a Cognito console user out everywhere, then delete them.

    API Gateway's JWT authorizer checks only a token's signature and expiry, so
    a deleted user's issued tokens keep passing it until they expire; it is the
    live group lookup (UserNotFoundException) that denies them here. Global
    sign-out first revokes their refresh and access tokens at Cognito, so
    nothing that asks Cognito about those tokens keeps honouring them either.
    A failed sign-out does not stop the delete, which is the stronger
    revocation; the response reports ``signedOut: false`` instead.
    """
    if not COGNITO_USER_POOL_ID:
        return _json(400, {"error": "COGNITO_USER_POOL_ID not configured"})
    username = params["username"]
    cognito = boto3.client("cognito-idp", region_name=REGION)
    signed_out = True
    try:
        cognito.admin_user_global_sign_out(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
    except Exception:
        signed_out = False
        logger.exception("global_sign_out_failed")
    try:
        cognito.admin_delete_user(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
    except Exception:
        return _error(500, "Delete user failed", signedOut=signed_out)
    forget_user(username)
    return _json(200, {"deleted": True, "username": username, "signedOut": signed_out})


def handle_list_users(_params, _body):
    """List Cognito admin users."""
    if not COGNITO_USER_POOL_ID:
        return _json(400, {"error": "COGNITO_USER_POOL_ID not configured"})
    try:
        cognito = boto3.client("cognito-idp", region_name=REGION)
        resp = cognito.list_users(UserPoolId=COGNITO_USER_POOL_ID, Limit=60)
        users = []
        for u in resp.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            users.append({
                "username": u["Username"],
                "email": attrs.get("email", ""),
                "status": u.get("UserStatus", ""),
                "enabled": u.get("Enabled", False),
                "created": str(u.get("UserCreateDate", "")),
                "lastModified": str(u.get("UserLastModifiedDate", "")),
            })
        return _json(200, users)
    except Exception:
        return _error(500, "List users failed")



@app.get("/api/users")
@require_group("operator")
def list_users():
    return from_legacy(handle_list_users({}, {}))


@app.put("/api/users")
@require_group("admin")
def create_user():
    return from_legacy(handle_create_user({}, current_body()))


@app.delete("/api/users/<username>")
@require_group("admin")
def delete_user(username: str):
    return from_legacy(handle_delete_user({"username": username}, {}))
