# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the env-var-key denylist and the state-table-name
validation hardening. Both reject before any AWS call, so we only need to
stub the registry lookup."""
import json
from unittest.mock import patch

import pytest

# NOTE: route modules import shared.config at import time, which requires env
# vars set by the autouse `aws_env` fixture — so import inside each test, not at
# module top level (mirrors tests/unit/test_conversations_pagination.py).


def _body(resp):
    return json.loads(resp["body"])


# ---------- env-var-key denylist in handle_update_config ----------

@pytest.mark.unit
@pytest.mark.parametrize("bad_key", [
    "AWS_LAMBDA_EXEC_WRAPPER", "AWS_ACCESS_KEY_ID", "aws_secret_access_key",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP", "pythonpath",
])
def test_update_config_rejects_reserved_env_key(bad_key):
    from routes import stacks
    with patch.object(stacks, "_get_registry", return_value={"outputs": {"webhook_lambda": "dev-socialagent-webhook"}}):
        resp = stacks.handle_update_config({"id": "socialagent-dev"}, {"environmentVariables": {bad_key: "x"}})
    assert resp["statusCode"] == 400
    assert "Reserved environment variable keys" in _body(resp)["error"]


@pytest.mark.unit
def test_update_config_rejects_when_any_key_reserved():
    from routes import stacks
    with patch.object(stacks, "_get_registry", return_value={"outputs": {"webhook_lambda": "dev-socialagent-webhook"}}):
        resp = stacks.handle_update_config(
            {"id": "socialagent-dev"},
            {"environmentVariables": {"LOG_LEVEL": "DEBUG", "LD_PRELOAD": "/evil.so"}},
        )
    assert resp["statusCode"] == 400
    assert "LD_PRELOAD" in _body(resp)["error"]


@pytest.mark.unit
def test_update_config_allows_safe_keys():
    # No reserved key -> passes validation and reaches the (mocked) Lambda loop.
    from routes import stacks
    with patch.object(stacks, "_get_registry", return_value={"outputs": {"webhook_lambda": "dev-socialagent-webhook"}}), \
         patch.object(stacks, "lambda_client") as lc:
        lc.get_function_configuration.return_value = {"Environment": {"Variables": {}}}
        resp = stacks.handle_update_config(
            {"id": "socialagent-dev"},
            {"environmentVariables": {"LOG_LEVEL": "DEBUG", "FEATURE_FLAG": "on"}},
        )
    assert resp["statusCode"] == 200


# ---------- state_table_name validation ----------

@pytest.mark.unit
@pytest.mark.parametrize("name,ok", [
    ("socialagent-dev-state", True),
    ("personal-assistant-prod-state", True),
    ("a-state", True),
    ("", False),
    ("arbitrary-table", False),
    ("socialagent-dev-state-evil", False),
    ("some-other-settings", False),
    ("state", False),                 # no prefix before -state
    ("socialagent dev state", False),        # spaces
    ("../secret-state", False),       # path chars
    ("x" * 260 + "-state", False),    # too long
])
def test_is_valid_state_table(name, ok):
    from routes import state
    assert state._is_valid_state_table(name) is ok


@pytest.mark.unit
def test_get_state_rejects_bad_table_name():
    from routes import state
    with patch.object(state, "_get_registry", return_value={"outputs": {"state_table_name": "arbitrary-table"}}):
        resp = state.handle_get_state({"id": "socialagent-dev"}, {})
    assert resp["statusCode"] == 400
    assert _body(resp)["error"] == "Invalid state table name"


@pytest.mark.unit
def test_delete_state_key_rejects_bad_table_name():
    from routes import state
    with patch.object(state, "_get_registry", return_value={"outputs": {"state_table_name": "victim-settings"}}):
        resp = state.handle_delete_state_key({"id": "socialagent-dev"}, {"phone": "+1", "key": "k"})
    assert resp["statusCode"] == 400
    assert _body(resp)["error"] == "Invalid state table name"


@pytest.mark.unit
def test_list_all_state_skips_bad_table_names():
    # A registry entry with a bogus table name is skipped, not fatal.
    from routes import state
    regs = {"socialagent-dev": {"outputs": {"state_table_name": "not-a-valid-name"}}}
    with patch.object(state, "_get_all_registries", return_value=regs):
        resp = state.handle_list_all_state({}, {})
    assert resp["statusCode"] == 200
    assert _body(resp) == []


# ── logs: malformed minutes must not crash the handler ─────────────────────────


import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("bad", ["abc", None, [1], {"x": 1}])
def test_logs_minutes_coercion_is_guarded(bad):
    """A non-int `minutes` must fall back, not fail the invocation with a 500."""
    from unittest.mock import patch
    from routes import logs

    with patch.object(logs, "get_all_registries", return_value={}), patch.object(
        logs, "client"
    ):
        resp = logs.search_logs_impl({"minutes": bad})
    assert resp["statusCode"] == 200


# ---------- template name validation ----------


# The console reads the name from conversation text an end user can write.
# "..%2F..%2Fusers" is what the resolver hands the route for an encoded "../../users".
@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_name",
    [
        "..%2F..%2Fusers",
        "../users",
        "..",
        "Welcome",
        "welcome?page_size=100",
        "welcome\n",
        "",
        "x" * 513,
    ],
)
def test_template_detail_rejects_invalid_name(bad_name):
    from routes import templates

    with (
        patch.object(templates, "_get_registry") as reg,
        patch.object(templates, "boto3") as b3,
    ):
        resp = templates.handle_get_template_detail(
            {"id": "bot-dev", "template_name": bad_name}, {}
        )
    assert resp["statusCode"] == 400
    assert _body(resp) == {"error": "Invalid template name"}
    reg.assert_not_called()
    b3.client.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("name", ["dev_socialagent_welcome", "welcome", "x" * 512])
def test_template_detail_accepts_meta_names(name):
    from routes import templates

    registry = {"region": "us-east-1", "phone_number_id": "phone-number-id-01"}
    with (
        patch.object(templates, "_get_registry", return_value=registry),
        patch.object(templates, "boto3") as b3,
    ):
        eum = b3.client.return_value
        eum.get_linked_whatsapp_business_account_phone_number.return_value = {
            "linkedWhatsAppBusinessAccountId": "waba-1"
        }
        eum.list_whatsapp_message_templates.return_value = {"templates": []}
        resp = templates.handle_get_template_detail(
            {"id": "bot-dev", "template_name": name}, {}
        )
    assert resp["statusCode"] == 404
    assert _body(resp) == {"error": f"Template '{name}' not found"}
    eum.list_whatsapp_message_templates.assert_called_once_with(
        id="waba-1", maxResults=100
    )
