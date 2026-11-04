# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PUT /api/stacks/<id>/trigger-event sends credentials only to API Gateway.

The handler attaches the stack's Events API key and webhook secret to a POST to
the registry's ``outputs.event_api_url``. It checked only for https, so a
registry entry naming any other host received both secrets. The host must now
be an ``execute-api`` endpoint before a credential is even fetched.
"""

import io
import json
from unittest.mock import MagicMock

import pytest

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.

GOOD_URL = "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/prod/events"
API_ID = GOOD_URL.removeprefix("https://").split(".", 1)[0]


class _Opener:
    def __init__(self, open_fn):
        self.open = open_fn


def _use_opener(monkeypatch, open_fn):
    """Route the handler's no-redirect opener to ``open_fn(req, timeout)``."""
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: _Opener(open_fn))


@pytest.fixture
def stack(monkeypatch):
    """A stack whose credentials resolve, plus a recorder for outbound requests."""
    from routes import events

    apigw, sm = MagicMock(), MagicMock()
    apigw.get_api_key.return_value = {"value": "api-key-value"}
    apigw.get_usage_plans.return_value = {"items": [{"apiStages": [{"apiId": API_ID, "stage": "prod"}]}]}
    sm.get_secret_value.return_value = {"SecretString": json.dumps({"webhook_secret": "s3cret"})}
    monkeypatch.setattr(
        events.boto3, "client", lambda service, **k: {"apigateway": apigw, "secretsmanager": sm}[service]
    )

    sent = []

    def fake_urlopen(req, timeout):
        sent.append(req)
        resp = MagicMock(status=200)
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__.return_value = resp
        return resp

    _use_opener(monkeypatch, fake_urlopen)

    def trigger(url):
        config = {
            "project": "bot",
            "environment": "dev",
            "region": "us-east-1",
            "outputs": {
                "event_api_url": url,
                "event_api_key_id": "key-id",
                "event_api_webhook_secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s",  # pragma: allowlist secret
            },
        }
        monkeypatch.setattr(events, "_get_registry", lambda _id: config)
        resp = events.handle_trigger_event({"id": "bot-dev"}, {"payload": {"x": 1}})
        return resp["statusCode"], json.loads(resp["body"])

    return trigger, sent, apigw, sm


def test_execute_api_endpoint_gets_the_credentials(stack):
    trigger, sent, _, _ = stack

    status, body = trigger(GOOD_URL)

    assert status == 200 and body["status"] == 200
    assert [r.full_url for r in sent] == [GOOD_URL]
    assert sent[0].get_header("X-api-key") == "api-key-value"
    assert sent[0].get_header("X-webhook-secret") == "s3cret"


@pytest.mark.parametrize(
    "url",
    [
        "https://collector.example.com/events",
        "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com.example.com/events",
        "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com:8443/events",
        "https://user@a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/events",
        "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com@example.com/events",
        "https://execute-api.us-east-1.amazonaws.com/events",
        "https://a1b2c3d4e5.lambda-url.us-east-1.on.aws/",
        "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com./events",
    ],
)
def test_any_other_host_is_refused_before_credentials_are_read(stack, url):
    trigger, sent, apigw, sm = stack

    status, _ = trigger(url)

    assert status == 400
    assert sent == []
    apigw.get_api_key.assert_not_called()
    sm.get_secret_value.assert_not_called()


def test_plain_http_is_still_refused(stack):
    trigger, sent, _, _ = stack

    status, _ = trigger(GOOD_URL.replace("https://", "http://"))

    assert status == 400
    assert sent == []


def test_http_error_from_the_event_api_is_relayed(stack, monkeypatch):
    """The upstream response body is the event API's own, not exception text."""
    import urllib.error

    trigger, _, _, _ = stack

    def failing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"no"}'))

    _use_opener(monkeypatch, failing)
    status, body = trigger(GOOD_URL)

    assert status == 200
    assert body["status"] == 403
    assert body["response"] == '{"message":"no"}'


def test_an_endpoint_in_another_region_is_refused(stack):
    trigger, sent, apigw, sm = stack

    status, body = trigger(GOOD_URL.replace("us-east-1", "eu-west-1"))

    assert status == 400 and "region" in body["error"]
    assert sent == []
    apigw.get_usage_plans.assert_not_called()
    sm.get_secret_value.assert_not_called()


def test_an_api_the_key_is_not_bound_to_never_gets_the_credentials(stack):
    """The host shape matches, but the stack's key belongs to another API.

    That covers an API in another account and another stack's API here.
    """
    trigger, sent, apigw, sm = stack
    apigw.get_usage_plans.return_value = {"items": [{"apiStages": [{"apiId": "otherapi99", "stage": "prod"}]}]}

    status, body = trigger(GOOD_URL)

    assert status == 400 and "API key belongs to" in body["error"]
    apigw.get_usage_plans.assert_called_once_with(keyId="key-id", limit=100)
    assert sent == []
    apigw.get_api_key.assert_not_called()
    sm.get_secret_value.assert_not_called()


def test_a_failed_binding_lookup_refuses_the_send(stack):
    from botocore.exceptions import ClientError

    trigger, sent, apigw, sm = stack
    apigw.get_usage_plans.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetUsagePlans")

    status, body = trigger(GOOD_URL)

    # A lookup failure is not a wrong URL: it says what failed, without the
    # exception text, and nothing is read or sent.
    assert status == 502 and body["error"].startswith("Could not verify which API")
    assert "AccessDenied" not in json.dumps(body)
    assert sent == []
    apigw.get_api_key.assert_not_called()
    sm.get_secret_value.assert_not_called()


def test_the_binding_lookup_follows_pagination(stack):
    trigger, sent, apigw, _ = stack
    apigw.get_usage_plans.side_effect = [
        {"items": [{"apiStages": [{"apiId": "otherapi99"}]}], "position": "p2"},
        {"items": [{"apiStages": [{"apiId": API_ID}]}]},
    ]

    status, body = trigger(GOOD_URL)

    assert status == 200 and body["status"] == 200
    assert [r.full_url for r in sent] == [GOOD_URL]


def test_redirects_are_not_followed():
    """Following a redirect would replay the credential headers to the new host."""
    import urllib.request

    from routes import events

    handler = events._NoRedirect()
    req = urllib.request.Request(GOOD_URL, data=b"{}", method="POST", headers={"x-webhook-secret": "s"})
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://collector.example.com/") is None


def test_an_entry_without_a_key_id_reads_no_credential(stack, monkeypatch):
    """Without event_api_key_id the URL's binding cannot be checked, so stop first."""
    from routes import events

    _, sent, apigw, sm = stack
    config = {
        "project": "bot",
        "environment": "dev",
        "region": "us-east-1",
        "outputs": {"event_api_url": GOOD_URL},
    }
    monkeypatch.setattr(events, "_get_registry", lambda _id: config)

    resp = events.handle_trigger_event({"id": "bot-dev"}, {"payload": {}})
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200 and body["status"] == 412
    assert json.loads(body["response"])["missing"] == ["x-api-key"]
    assert sent == []
    apigw.get_usage_plans.assert_not_called()
    apigw.get_api_key.assert_not_called()
    sm.get_secret_value.assert_not_called()
    sm.describe_secret.assert_not_called()
