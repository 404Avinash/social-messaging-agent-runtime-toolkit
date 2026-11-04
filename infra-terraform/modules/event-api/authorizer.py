# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""API Gateway Lambda authorizer — validates webhook secret from header."""

import json
import os
import secrets
import boto3

_SECRET_ARN = os.environ["SECRET_ARN"]
_HEADER_NAME = os.environ.get("AUTH_HEADER_NAME", "x-webhook-secret").lower()

_cached_secret = None


def _get_secret():
    global _cached_secret
    if _cached_secret is None:
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=_SECRET_ARN)
        _cached_secret = json.loads(resp["SecretString"]).get("webhook_secret", "")
    return _cached_secret


def handler(event, context):
    # A REST API REQUEST authorizer gets header names in the case the client
    # sent them (urllib sends "X-webhook-secret"), and HTTP header names are
    # case-insensitive, so match on the lowercased name.
    sent = event.get("headers") or {}
    headers = {name.lower(): value for name, value in sent.items()}
    token = headers.get(_HEADER_NAME, "")
    effect = (
        "Allow" if token and secrets.compare_digest(token, _get_secret()) else "Deny"
    )
    return {
        "principalId": "webhook-caller",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": event["methodArn"],
                }
            ],
        },
    }
