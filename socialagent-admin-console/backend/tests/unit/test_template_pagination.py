# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The template routes must read every page EUM Social returns.

ListWhatsAppMessageTemplates and ListLinkedWhatsAppBusinessAccounts return at
most 100 items a page and a nextToken while more remain. A route that reads
only the first page lists part of a large WABA, answers 404 for a template that
exists and leaves templates out of the health counts.
"""
import json
from unittest.mock import call, patch

import pytest

# Route modules read env vars at import time, set by conftest's autouse aws_env
# fixture, so import inside each test (as tests/unit/test_input_validation.py does).

REGISTRY = {
    "project": "bot",
    "environment": "dev",
    "region": "us-east-1",
    "phone_number_id": "phone-number-id-01",
    "template_prefix": "dev_bot_",
}


def _body(resp):
    return json.loads(resp["body"])


def _template(name, meta_id, status="APPROVED"):
    return {
        "templateName": name,
        "metaTemplateId": meta_id,
        "templateStatus": status,
        "templateLanguage": "en_US",
        "templateCategory": "UTILITY",
    }


# Page 1 ends with a nextToken; the template the tests look for is on page 2.
PAGE_1 = {
    "templates": [_template("dev_bot_welcome", "meta-1"), _template("other_app_hello", "meta-2")],
    "nextToken": "page-2",
}
PAGE_2 = {"templates": [_template("dev_bot_reminder", "meta-3", status="PENDING")]}
TWO_PAGE_CALLS = [
    call(id="waba-1", maxResults=100),
    call(id="waba-1", maxResults=100, nextToken="page-2"),
]


def _stub_eum(b3, *template_pages, waba_id="waba-1"):
    eum = b3.client.return_value
    eum.get_linked_whatsapp_business_account_phone_number.return_value = {
        "linkedWhatsAppBusinessAccountId": waba_id
    }
    eum.list_whatsapp_message_templates.side_effect = list(template_pages)
    eum.get_whatsapp_message_template.return_value = {"template": "{}"}
    return eum


@pytest.mark.unit
def test_template_list_includes_later_pages():
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, PAGE_1, PAGE_2)
        resp = templates.handle_get_templates({"id": "bot-dev"}, {})
    assert resp["statusCode"] == 200
    assert [t["name"] for t in _body(resp)] == ["dev_bot_welcome", "dev_bot_reminder"]
    assert eum.list_whatsapp_message_templates.call_args_list == TWO_PAGE_CALLS


@pytest.mark.unit
def test_template_detail_finds_a_template_on_a_later_page():
    from routes import templates

    detail = {"name": "dev_bot_reminder", "category": "UTILITY", "status": "PENDING", "components": []}
    with (
        patch.object(templates, "_get_registry", return_value=REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, PAGE_1, PAGE_2)
        eum.get_whatsapp_message_template.return_value = {"template": json.dumps(detail)}
        resp = templates.handle_get_template_detail(
            {"id": "bot-dev", "template_name": "dev_bot_reminder"}, {}
        )
    assert resp["statusCode"] == 200
    assert _body(resp)["name"] == "dev_bot_reminder"
    assert _body(resp)["language"] == "en_US"
    assert eum.list_whatsapp_message_templates.call_args_list == TWO_PAGE_CALLS
    eum.get_whatsapp_message_template.assert_called_once_with(metaTemplateId="meta-3", id="waba-1")


@pytest.mark.unit
def test_template_detail_stops_paginating_once_found():
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, PAGE_1, PAGE_2)
        resp = templates.handle_get_template_detail(
            {"id": "bot-dev", "template_name": "dev_bot_welcome"}, {}
        )
    assert resp["statusCode"] == 200
    eum.list_whatsapp_message_templates.assert_called_once_with(id="waba-1", maxResults=100)


@pytest.mark.unit
def test_template_detail_still_404s_after_the_last_page():
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, PAGE_1, PAGE_2)
        resp = templates.handle_get_template_detail(
            {"id": "bot-dev", "template_name": "dev_bot_missing"}, {}
        )
    assert resp["statusCode"] == 404
    assert eum.list_whatsapp_message_templates.call_args_list == TWO_PAGE_CALLS


@pytest.mark.unit
def test_template_health_counts_later_pages():
    from routes import templates

    with (
        patch.object(templates, "_get_all_registries", return_value={"bot-dev": REGISTRY}),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, PAGE_1, PAGE_2)
        resp = templates.handle_get_template_health({}, {})
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["total"] == 2
    assert body["nonApproved"] == [
        {"name": "dev_bot_reminder", "stack": "bot-dev", "approvalStatus": "pending"}
    ]
    assert eum.list_whatsapp_message_templates.call_args_list == TWO_PAGE_CALLS


@pytest.mark.unit
@pytest.mark.parametrize("handler", ["handle_get_templates", "handle_get_template_detail"])
def test_waba_fallback_reads_past_an_empty_first_page(handler):
    """With no WABA linked to the phone, the fallback must follow nextToken too."""
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = _stub_eum(b3, {"templates": [_template("dev_bot_welcome", "meta-1")]}, waba_id="")
        eum.list_linked_whatsapp_business_accounts.side_effect = [
            {"linkedAccounts": [], "nextToken": "waba-page-2"},
            {"linkedAccounts": [{"id": "waba-1"}]},
        ]
        resp = getattr(templates, handler)({"id": "bot-dev", "template_name": "dev_bot_welcome"}, {})
    assert resp["statusCode"] == 200
    assert eum.list_linked_whatsapp_business_accounts.call_args_list == [
        call(maxResults=100),
        call(maxResults=100, nextToken="waba-page-2"),
    ]
    eum.list_whatsapp_message_templates.assert_called_once_with(id="waba-1", maxResults=100)
