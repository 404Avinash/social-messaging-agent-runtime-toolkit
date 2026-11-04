# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PUT /api/settings must not let the caller choose where it writes.

The item was built as ``{"pk": "SETTINGS", "sk": "admin", **body}`` — the spread
last, so a request body carrying ``pk``/``sk`` redirected the write. The real
settings row was then never updated while the response still said "saved".
"""

import json
from unittest.mock import MagicMock

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.


def _call(body: dict):
    """Invoke handle_update_settings with a stubbed table; return (table, resp)."""
    from routes import settings

    table = MagicMock()
    original = settings.settings_table
    settings.settings_table = table
    try:
        resp = settings.handle_update_settings(None, body)
    finally:
        settings.settings_table = original
    return table, resp


def test_client_cannot_redirect_the_write():
    """A body carrying pk/sk must not move the write off SETTINGS/admin."""
    table, resp = _call(
        {"pk": "PROBE_COUNT", "sk": "dev-socialagent-webhook", "slackWebhookUrl": "x"}
    )

    assert resp["statusCode"] == 200
    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "SETTINGS"
    assert item["sk"] == "admin"
    # The caller's payload still lands, minus the keys it does not own.
    assert item["slackWebhookUrl"] == "x"


def test_ordinary_settings_still_save():
    table, resp = _call({"slackWebhookUrl": "https://example.invalid/hook"})

    assert resp["statusCode"] == 200
    item = table.put_item.call_args.kwargs["Item"]
    assert item == {
        "pk": "SETTINGS",
        "sk": "admin",
        "slackWebhookUrl": "https://example.invalid/hook",
    }


def test_unsupported_value_type_is_a_client_error_not_a_502():
    """DynamoDB rejects floats; that must not escape as a Lambda crash.

    An unhandled exception here becomes an API Gateway 502 with no JSON body and
    a traceback in the admin log group.
    """
    from routes import settings

    table = MagicMock()
    table.put_item.side_effect = TypeError("Float types are not supported")
    original = settings.settings_table
    settings.settings_table = table
    try:
        resp = settings.handle_update_settings(None, {"degradationAlerts": 1.5})
    finally:
        settings.settings_table = original

    assert resp["statusCode"] >= 400
    assert "error" in json.loads(resp["body"])
