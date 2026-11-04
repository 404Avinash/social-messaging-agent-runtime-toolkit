# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for TemplateRegistry.
"""

import textwrap
from pathlib import Path

import pytest

from socialagent.templates.registry import TemplateRegistry

_YAML = textwrap.dedent("""\
    templates:
      - name: appointment_confirmed
        category: UTILITY
        language: es_MX
        components:
          - type: BODY
            text: "Tu cita para el {{1}} es el {{2}} a las {{3}}."
            variables:
              - position: 1
                name: car
              - position: 2
                name: date
                format: date
              - position: 3
                name: time
                format: time
      - name: payment_receipt
        category: UTILITY
        language: es_MX
        components:
          - type: BODY
            text: "Tu pago de {{1}} fue procesado."
            variables:
              - position: 1
                name: amount
                format: currency
""")


@pytest.fixture
def registry(tmp_path: Path) -> TemplateRegistry:
    p = tmp_path / "templates.yaml"
    p.write_text(_YAML)
    return TemplateRegistry(p)


def test_names(registry: TemplateRegistry) -> None:
    assert set(registry.names) == {"appointment_confirmed", "payment_receipt"}


def test_get(registry: TemplateRegistry) -> None:
    tmpl = registry.get("appointment_confirmed")
    assert tmpl["category"] == "UTILITY"


def test_get_missing(registry: TemplateRegistry) -> None:
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_render_plain(registry: TemplateRegistry) -> None:
    result = registry.render(
        "appointment_confirmed", ["Downtown Loft", "2026-02-10", "09:00"]
    )
    assert "Downtown Loft" in result
    assert "10 de febrero de 2026" in result
    assert "09:00" in result


def test_render_currency(registry: TemplateRegistry) -> None:
    result = registry.render("payment_receipt", [500])
    assert "$500.00" in result


def test_render_wrong_param_count(registry: TemplateRegistry) -> None:
    with pytest.raises(ValueError, match="expects 3 params"):
        registry.render("appointment_confirmed", ["only one"])


def test_comment_only_yaml_loads_empty(tmp_path: Path) -> None:
    """An empty or comment-only templates.yaml must load, not crash.

    yaml.safe_load returns None for such a file; `None.get(...)` crashed the
    registry at Lambda cold start, where all four handlers build it at import. A
    user starting a templates.yaml from scratch hits this before the first entry.
    """
    p = tmp_path / "templates.yaml"
    p.write_text("# no templates yet\n")
    assert TemplateRegistry(p).names == []


def test_empty_yaml_loads_empty(tmp_path: Path) -> None:
    p = tmp_path / "templates.yaml"
    p.write_text("")
    assert TemplateRegistry(p).names == []
