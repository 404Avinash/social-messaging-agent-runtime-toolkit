# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The console must derive the same template prefix Terraform registered.

Terraform builds it as
``"${var.environment}_${replace(var.project_name, "-", "_")}_"``
(``infra-terraform/locals.tf:22,36``). A fallback of ``f"{environment}_{project}_"``
without the hyphen replacement would filter out every template, and double-prefix
every send, for stacks whose project name contains a hyphen.
"""

import json
from unittest.mock import patch

import pytest

# shared.registry reads required env vars at import time, and conftest's aws_env
# fixture only runs once collection is done — so import inside each test.


@pytest.mark.parametrize(
    "config,expected",
    [
        # The explicit value always wins, whatever the project name looks like.
        ({"template_prefix": "custom_", "environment": "dev", "project": "b"}, "custom_"),
        # Fallback must mirror Terraform's replace(project_name, "-", "_").
        ({"environment": "dev", "project": "my-bot"}, "dev_my_bot_"),
        ({"environment": "prod", "project": "car-viewings"}, "prod_car_viewings_"),
        ({"environment": "dev", "project": "personal-assistant"}, "dev_personal_assistant_"),
        # Multiple hyphens, and names that need no replacement at all.
        ({"environment": "dev", "project": "a-b-c"}, "dev_a_b_c_"),
        ({"environment": "dev", "project": "socialagent"}, "dev_socialagent_"),
    ],
)
def test_prefix_matches_terraform(config, expected):
    from shared.registry import template_prefix_for

    assert template_prefix_for(config) == expected


def test_hyphenated_template_name_matches_its_own_prefix():
    """The end-to-end symptom: a real registered name must match the prefix.

    Terraform registers `dev_my_bot_reminder` for project `my-bot`. The old
    fallback produced `dev_my-bot_`, which that name does not start with — so the
    Templates page came back empty and the reclassification badge never fired.
    """
    from shared.registry import template_prefix_for

    prefix = template_prefix_for({"environment": "dev", "project": "my-bot"})
    assert "dev_my_bot_reminder".startswith(prefix)


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"project": "my-bot"}, "my_bot_"),
        ({"project": "car-viewings"}, "car_viewings_"),
        ({"project": "socialagent"}, "socialagent_"),
        ({}, ""),
    ],
)
def test_base_prefix_also_replaces_hyphens(config, expected):
    """The secondary project-only prefix needs the same replacement.

    Both templates.py (matching) and settings.py (stripping) used a raw
    f"{project}_", so a hyphenated project failed the second match too.
    """
    from shared.registry import template_base_prefix_for

    assert template_base_prefix_for(config) == expected


def test_empty_prefix_when_config_is_bare():
    """A registry with neither key yields a bare prefix, not a broken one.

    `"_"` would silently match nothing; an empty string at least makes the
    missing-configuration case obvious to the caller.
    """
    from shared.registry import template_prefix_for

    assert template_prefix_for({}) == ""


_MY_BOT = {
    "project": "my-bot",
    "environment": "dev",
    "region": "us-east-1",
    "phone_number_id": "phone-number-id-01",
    "template_prefix": "dev_my_bot_",
}


def _waba(b3, *templates):
    """Stub EUM Social: one WABA holding *templates*.

    Each is ``(name, status, detail)``, the listing's category taken from the
    detail's, or ``(name, status, detail, listing_category)``.
    """
    templates = [
        tpl if len(tpl) == 4 else (*tpl, tpl[2].get("category", "UTILITY"))
        for tpl in templates
    ]
    eum = b3.client.return_value
    eum.get_linked_whatsapp_business_account_phone_number.return_value = {
        "linkedWhatsAppBusinessAccountId": "waba-1"
    }
    eum.list_whatsapp_message_templates.return_value = {
        "templates": [
            {
                "templateName": name,
                "metaTemplateId": f"meta-{name}",
                "templateStatus": status,
                "templateCategory": category,
            }
            for name, status, _, category in templates
        ]
    }
    details = {f"meta-{name}": detail for name, _, detail, _ in templates}
    eum.get_whatsapp_message_template.side_effect = lambda id, metaTemplateId: {
        "template": json.dumps(details[metaTemplateId])
    }


def test_health_matches_template_names_as_the_list_does():
    """Health takes a stack's templates by name as its Templates page does.

    The list also matches project-only names, kept for templates registered
    without the environment segment; health matched the stack's prefix alone,
    so a reclassified project-only template the Templates page flagged never
    reached the Overview's reclassification alert, and a rejected one was left
    out of ``nonApproved``.
    """
    from routes import templates

    reclassified = {"category": "MARKETING", "previous_category": "UTILITY"}
    waba = [
        ("dev_my_bot_welcome", "APPROVED", {}),
        ("my_bot_reminder", "REJECTED", reclassified),
        ("other_app_hello", "PENDING", {}),
    ]
    with (
        patch.object(templates, "_get_registry", return_value=_MY_BOT),
        patch.object(templates, "_get_all_registries", return_value={"my-bot-dev": _MY_BOT}),
        patch.object(templates, "boto3") as b3,
    ):
        _waba(b3, *waba)
        listed = json.loads(templates.handle_get_templates({"id": "my-bot-dev"}, {})["body"])
        health = json.loads(templates.handle_get_template_health({}, {})["body"])

    assert [t["name"] for t in listed] == ["dev_my_bot_welcome", "my_bot_reminder"]
    assert health["total"] == len(listed)
    assert health["nonApproved"] == [
        {"name": "my_bot_reminder", "stack": "my-bot-dev", "approvalStatus": "rejected"}
    ]
    assert health["reclassified"] == [
        {
            "name": "my_bot_reminder",
            "stack": "my-bot-dev",
            "category": "MARKETING",
            "previousCategory": "UTILITY",
        }
    ]


@pytest.mark.parametrize("config", [{}, {"environment": "dev"}])
def test_a_config_without_a_prefix_lists_no_template(config):
    """No prefix is no match: ``startswith("")`` would claim the whole WABA."""
    from routes import templates
    from shared.registry import template_matches_stack

    assert not template_matches_stack("other_app_hello", config)
    registry = {**config, "region": "us-east-1", "phone_number_id": "phone-number-id-01"}
    with (
        patch.object(templates, "_get_registry", return_value=registry),
        patch.object(templates, "_get_all_registries", return_value={"bare": registry}),
        patch.object(templates, "boto3") as b3,
    ):
        _waba(b3, ("other_app_hello", "PENDING", {}))
        listed = json.loads(templates.handle_get_templates({"id": "bare"}, {})["body"])
        health = json.loads(templates.handle_get_template_health({}, {})["body"])

    assert listed == []
    assert health == {
        "reclassified": [],
        "nonApproved": [],
        "total": 0,
        "unchecked": [],
        "failedStacks": [],
    }


def test_project_only_config_lists_only_project_templates():
    """An entry with a project and no environment matches its project-only names.

    Without a published prefix it has no full one, and ``startswith("")``
    claimed every template in the WABA; it still has the project-only prefix.
    """
    from routes import templates
    from shared.registry import template_matches_stack

    config = {"project": "my-bot"}
    assert not template_matches_stack("other_app_hello", config)
    assert template_matches_stack("my_bot_x", config)
    registry = {**config, "region": "us-east-1", "phone_number_id": "phone-number-id-01"}
    with (
        patch.object(templates, "_get_registry", return_value=registry),
        patch.object(templates, "_get_all_registries", return_value={"my-bot": registry}),
        patch.object(templates, "boto3") as b3,
    ):
        _waba(b3, ("other_app_hello", "PENDING", {}), ("my_bot_x", "PENDING", {}))
        listed = json.loads(templates.handle_get_templates({"id": "my-bot"}, {})["body"])
        health = json.loads(templates.handle_get_template_health({}, {})["body"])

    assert [t["name"] for t in listed] == ["my_bot_x"]
    assert health["total"] == 1


def test_list_and_health_read_a_templates_category_the_same_way():
    """Both take the category from the template's detail, then the listing.

    The detail carries ``previous_category`` beside the category it moved to.
    The list compared it with the listing's ``templateCategory`` instead, so
    when the listing still held the old category it reported that one and no
    reclassification, while health flagged it for the Overview.
    """
    from routes import templates

    moved = {"category": "MARKETING", "previous_category": "UTILITY"}
    with (
        patch.object(templates, "_get_registry", return_value=_MY_BOT),
        patch.object(templates, "_get_all_registries", return_value={"my-bot-dev": _MY_BOT}),
        patch.object(templates, "boto3") as b3,
    ):
        _waba(b3, ("dev_my_bot_welcome", "APPROVED", moved, "UTILITY"))
        listed = json.loads(templates.handle_get_templates({"id": "my-bot-dev"}, {})["body"])
        health = json.loads(templates.handle_get_template_health({}, {})["body"])

    [template] = listed
    assert (template["category"], template["previousCategory"], template["reclassified"]) == (
        "MARKETING",
        "UTILITY",
        True,
    )
    assert health["reclassified"] == [
        {
            "name": "dev_my_bot_welcome",
            "stack": "my-bot-dev",
            "category": "MARKETING",
            "previousCategory": "UTILITY",
        }
    ]


def test_a_detail_that_is_not_an_object_reads_as_none():
    """A template whose detail JSON is not an object keeps its listing category.

    The list still shows it, unflagged but not checked, and health still
    counts it as unchecked, as when the detail cannot be read at all: the
    detail says nothing about whether Meta moved it.
    """
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=_MY_BOT),
        patch.object(templates, "_get_all_registries", return_value={"my-bot-dev": _MY_BOT}),
        patch.object(templates, "boto3") as b3,
    ):
        _waba(b3, ("dev_my_bot_welcome", "APPROVED", None, "UTILITY"))
        listed = json.loads(templates.handle_get_templates({"id": "my-bot-dev"}, {})["body"])
        health = json.loads(templates.handle_get_template_health({}, {})["body"])

    [template] = listed
    assert (
        template["category"],
        template["previousCategory"],
        template["reclassified"],
        template["checked"],
    ) == ("UTILITY", None, False, False)
    assert health == {
        "reclassified": [],
        "nonApproved": [],
        "total": 1,
        "unchecked": [{"name": "dev_my_bot_welcome", "stack": "my-bot-dev"}],
        "failedStacks": [],
    }
