# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deleting a state key takes the phone and key from the JSON body.

The state table's partition key is the E.164 phone, ``+`` included, and its sort
keys carry ``#`` (``counter#``, ``claim#``, the buffer's ``msg#``). In a path,
the console has to percent-encode both, and the resolver hands route parameters
over still encoded, so the old ``DELETE /state/<phone>/<key>`` route looked up
``%2B...``, matched nothing and still answered ``{"deleted": true}``. That route
is gone; ``POST /state/delete`` is the only way in.
"""

import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides, so import inside each test or fixture.

TABLE = "bot-dev-state"
PHONE = "+15555550100"


def _event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "version": "2.0",
        "routeKey": f"{method} /api/{{proxy+}}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body or {}),
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
            "authorizer": {"jwt": {"claims": {"sub": "test-user", "cognito:groups": "admin"}}},
            "requestId": "test-req-id",
            "routeKey": f"{method} /api/{{proxy+}}",
            "stage": "$default",
            "time": "08/May/2026:12:00:00 +0000",
            "timeEpoch": 1746700800000,
        },
        "isBase64Encoded": False,
    }


@pytest.fixture
def table(monkeypatch):
    import shared.auth as auth
    from routes import state

    # Authorize from the claim alone, so the test controls the caller's groups.
    monkeypatch.setattr(auth, "LIVE_GROUP_CHECK", False)
    monkeypatch.setattr(
        state, "_get_registry", lambda sid: {"region": "us-east-1", "outputs": {"state_table_name": TABLE}}
    )
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def _call(method: str, path: str, body: dict | None = None):
    from handler import handler

    ctx = MagicMock(function_name="admin-api", memory_limit_in_mb=256, aws_request_id="r")
    ctx.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:admin-api"
    resp = handler(_event(method, path, body), ctx)
    return resp["statusCode"], json.loads(resp["body"])


def _keys(table) -> set[tuple[str, str]]:
    return {(i["pk"], i["sk"]) for i in table.scan()["Items"]}


@pytest.mark.parametrize(
    "phone,key",
    [
        (PHONE, "booking"),
        (PHONE, "counter#replies"),
        (PHONE, "notes/2026"),
        (f"buf#{PHONE}", f"msg#{1:020d}#wamid.x"),
    ],
)
def test_body_route_deletes_the_exact_item(table, phone, key):
    table.put_item(Item={"pk": phone, "sk": key, "data": "{}"})
    table.put_item(Item={"pk": phone, "sk": "keep", "data": "{}"})

    status, body = _call("POST", "/api/stacks/bot-dev/state/delete", {"phone": phone, "key": key})

    assert status == 200
    assert body == {"deleted": True, "phone": phone, "key": key}
    assert _keys(table) == {(phone, "keep")}


@pytest.mark.parametrize("others", [[], [(PHONE, "keep"), ("+15555550101", "booking")]])
def test_body_route_says_when_there_was_nothing_to_delete(table, others):
    """DeleteItem succeeds on a key that is not there, so ``deleted`` comes
    from the item it returns, and the console can say the key was not found
    instead of reporting a delete that did not happen."""
    for pk, sk in others:
        table.put_item(Item={"pk": pk, "sk": sk, "data": "{}"})

    status, body = _call("POST", "/api/stacks/bot-dev/state/delete", {"phone": PHONE, "key": "booking"})

    assert status == 200
    assert body == {"deleted": False, "phone": PHONE, "key": "booking"}
    assert _keys(table) == set(others)


def test_the_path_form_is_gone(table):
    """A request in the old shape fails loudly instead of deleting nothing."""
    table.put_item(Item={"pk": PHONE, "sk": "booking", "data": "{}"})

    status, _ = _call("DELETE", "/api/stacks/bot-dev/state/%2B15555550100/booking")

    assert status == 404
    assert _keys(table) == {(PHONE, "booking")}


@pytest.mark.parametrize(
    "body",
    [
        {"phone": 15555550100, "key": "booking"},
        {"phone": PHONE, "key": ["booking"]},
        {"phone": PHONE},
        {"key": "booking"},
        {"phone": "", "key": "booking"},
        {"phone": PHONE, "key": ""},
    ],
)
def test_body_route_needs_a_string_phone_and_key(table, body):
    status, resp = _call("POST", "/api/stacks/bot-dev/state/delete", body)

    assert status == 400
    assert resp == {"error": "phone and key are required"}
