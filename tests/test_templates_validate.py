# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for local template validation."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from socialagent.templates.validate import (
    has_errors,
    validate_file,
    validate_template,
)

REPO = Path(__file__).resolve().parents[1]

GOOD = {
    "name": "visit_reminder",
    "category": "UTILITY",
    "language": "en",
    "components": [
        {
            "type": "BODY",
            "text": "You have an appointment to see the {{1}} at {{2}}. "
            "Reply here if you need to change the time.",
            "example": {"body_text": [["Parkview Apartment", "10:00 AM"]]},
        }
    ],
    "variables": [
        {"name": "property", "position": 1},
        {"name": "time", "position": 2},
    ],
}


def _with_body(text: str, n_vars: int | None = None) -> dict:
    t = copy.deepcopy(GOOD)
    t["components"][0]["text"] = text
    if n_vars is not None:
        t["variables"] = [
            {"name": f"v{i}", "position": i} for i in range(1, n_vars + 1)
        ]
        t["components"][0]["example"] = {"body_text": [["x"] * n_vars]}
    return t


def _messages(findings, severity: str) -> list[str]:
    return [f.message for f in findings if f.severity == severity]


def test_good_template_has_no_findings():
    assert validate_template(GOOD, prefix="dev_bot_") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{1}} is ready for pickup at our main office.", "opens on a variable"),
        ("Your order is ready for pickup, reference {{1}}", "closes on a variable"),
        ("Your order is ready for pickup, reference {{1}}.\n", None),
    ],
)
def test_variable_at_either_end_is_an_error(text, expected):
    errors = _messages(validate_template(_with_body(text, 1)), "error")
    if expected is None:
        assert errors == []
    else:
        assert any(expected in e for e in errors), errors


def test_doubled_prefix_is_an_error():
    t = copy.deepcopy(GOOD)
    t["name"] = "dev_bot_visit_reminder"
    errors = _messages(validate_template(t, prefix="dev_bot_"), "error")
    assert any("already starts with the prefix" in e for e in errors)


@pytest.mark.parametrize("name", ["Visit-Reminder", "visit reminder", "visitReminder"])
def test_invalid_name_is_an_error(name):
    t = copy.deepcopy(GOOD)
    t["name"] = name
    assert has_errors(validate_template(t))


def test_placeholders_out_of_sequence_are_an_error():
    t = _with_body("Your visit to see the {{1}} is booked for {{3}}, see you then.", 2)
    t["variables"][1]["position"] = 3
    errors = _messages(validate_template(t), "error")
    assert any("in sequence" in e for e in errors)


def test_variables_count_mismatch_is_an_error():
    t = copy.deepcopy(GOOD)
    t["variables"] = t["variables"][:1]
    errors = _messages(validate_template(t), "error")
    assert any("'variables' lists 1" in e for e in errors)


def test_missing_example_is_an_error():
    t = copy.deepcopy(GOOD)
    del t["components"][0]["example"]
    errors = _messages(validate_template(t), "error")
    assert any("example.body_text" in e for e in errors)


def test_bad_category_and_missing_language_are_errors():
    t = copy.deepcopy(GOOD)
    t["category"] = "TRANSACTIONAL"
    del t["language"]
    errors = _messages(validate_template(t), "error")
    assert any("category" in e for e in errors)
    assert any("language" in e for e in errors)


def test_low_static_text_per_variable_is_only_a_warning():
    # The one body Meta rejected: two variables, ~50 characters of static text.
    t = _with_body("Hi, your task {{1}} finished with result {{2}} ok", 2)
    findings = validate_template(t)
    assert not has_errors(findings)
    assert any("too many variables" in m for m in _messages(findings, "warning"))


def test_emoji_in_utility_is_a_warning_but_punctuation_is_not():
    emoji = _with_body("Your visit to see the {{1}} is confirmed 🚗 see you soon.", 1)
    assert any("emoji" in m for m in _messages(validate_template(emoji), "warning"))
    dash = _with_body(
        "We're sorry — your visit to see the {{1}} was moved, reply here.", 1
    )
    assert _messages(validate_template(dash), "warning") == []


def test_brand_name_in_utility_is_a_warning():
    t = _with_body("Acme here: your visit to see the {{1}} is confirmed, see you.", 1)
    warnings = _messages(validate_template(t, brand_name="Acme"), "warning")
    assert any("brand" in m for m in warnings)


def test_duplicate_names_are_an_error(tmp_path):
    other = copy.deepcopy(GOOD)
    other["language"] = "es_MX"
    path = tmp_path / "templates.yaml"
    path.write_text(yaml.safe_dump({"templates": [GOOD, other]}))
    assert any("duplicate" in f.message for f in validate_file(path))


def test_file_that_is_not_a_templates_document_raises(tmp_path):
    path = tmp_path / "templates.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        validate_file(path)


@pytest.mark.parametrize("blueprint", ["personal-assistant", "appointment-scheduler"])
def test_shipped_blueprint_templates_pass(blueprint):
    # Both were registered against a real WABA on 2026-09-14, so a validator
    # that blocks either one is wrong, not the template.
    path = REPO / "blueprints" / blueprint / "templates.yaml"
    findings = validate_file(path, prefix="dev_my_bot_")
    assert findings == [], [str(f) for f in findings]
