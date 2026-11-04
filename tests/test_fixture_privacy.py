# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Recorded fixtures carry no one's data.

A fixture under tests/fixtures/ may start as a real conversation exported from a
deployed stack. Before it is committed the recording is reduced to what the test
needs; this test is the mechanical half of that rule: no phone number, no email
and no URL may remain in a fixture's text. Names are checked by the release
tooling, which also scans the fixtures.
"""

import json
import re
from pathlib import Path

import pytest

FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures").glob("*.json"))

# The placeholders the docs use, which a fixture may legitimately contain.
ALLOWED_NUMBERS = {"15555550100", "525555550100", "5215500000000", "123456789012"}

# Tool-use ids are UUIDs whose hex tails look like phone numbers; drop them first.
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
PHONE = re.compile(r"\+?\d[\d ().-]{8,}\d")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
URL = re.compile(r"https?://\S+")


def _texts(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _texts(v)
    elif isinstance(node, str):
        yield node


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_fixture_has_no_contact_data(fixture):
    text = UUID.sub(
        " ", "\n".join(_texts(json.loads(fixture.read_text(encoding="utf-8"))))
    )
    phones = {re.sub(r"\D", "", m) for m in PHONE.findall(text)} - ALLOWED_NUMBERS
    phones = {p for p in phones if len(p) >= 10}
    assert not phones, f"{fixture.name}: phone-shaped numbers {sorted(phones)}"
    assert not EMAIL.findall(text), f"{fixture.name}: email addresses"
    assert not URL.findall(text), f"{fixture.name}: URLs"


def test_there_are_fixtures_to_check():
    assert FIXTURES, "tests/fixtures/*.json moved; update this test"
