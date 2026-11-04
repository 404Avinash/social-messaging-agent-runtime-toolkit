# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Template registry: loads templates from YAML, indexes by name, and renders parameters.

YAML format example::

    templates:
      - name: appointment_confirmed
        category: UTILITY
        language: es_MX
        components:
          - type: BODY
            text: "Tu cita para {{1}} es el {{2}} a las {{3}}."
            variables:
              - position: 1
                name: car
              - position: 2
                name: date
                format: date
              - position: 3
                name: time
                format: time
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

from socialagent.constants import MONTHS_ES


# ── Locale-aware formatters ───────────────────────────────────────────────────


def _fmt_date(value: Any) -> str:
    """Format a date value in Spanish long form (e.g. '10 de febrero de 2026').

    Accepts :class:`datetime.date`, :class:`datetime.datetime`, or an ISO string.
    """
    if isinstance(value, str):
        value = date.fromisoformat(value)
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.day} de {MONTHS_ES[value.month - 1]} de {value.year}"


def _fmt_time(value: Any) -> str:
    """Format a time value as HH:MM (24-hour).

    Accepts :class:`datetime.time`, :class:`datetime.datetime`, or an HH:MM string.
    """
    if isinstance(value, str):
        value = time.fromisoformat(value)
    if isinstance(value, datetime):
        value = value.time()
    return f"{value.hour:02d}:{value.minute:02d}"


def _fmt_currency(value: Any) -> str:
    """Format a numeric value as a currency string (e.g. '$1,234.56').

    Accepts int, float, or a string representation of a number.
    """
    return f"${float(value):,.2f}"


_FORMATTERS: dict[str, Any] = {
    "date": _fmt_date,
    "time": _fmt_time,
    "currency": _fmt_currency,
}

_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")


# ── Registry ──────────────────────────────────────────────────────────────────


class TemplateRegistry:
    """In-memory registry of WhatsApp message templates loaded from a YAML file.

    Templates are indexed by name for O(1) lookup. Parameter rendering is
    locale-aware via format annotations (``date``, ``time``, ``currency``).
    """

    def __init__(self, templates_path: str | Path) -> None:
        self._templates: dict[str, dict] = {}
        self._load(Path(templates_path))

    def _load(self, path: Path) -> None:
        """Load and index templates from the YAML file at ``path``."""
        with path.open(encoding="utf-8") as fh:
            # `or {}`: an empty or comment-only templates.yaml parses to None, and
            # `None.get(...)` crashed the registry at Lambda cold start — all four
            # handlers build it at module import, so every invocation failed. A
            # user starting a templates.yaml from scratch (blueprints/minimal ships
            # none) hits this before adding the first entry.
            data = yaml.safe_load(fh) or {}
        for tmpl in data.get("templates", []):
            self._templates[tmpl["name"]] = tmpl

    def get(self, name: str) -> dict:
        """Return the raw template dict for ``name``.

        Raises :class:`KeyError` if the template is not registered.
        """
        return self._templates[name]

    def render(self, name: str, params: list[Any]) -> str:
        """Render the BODY component of a template with the given parameters.

        Parameters are formatted according to their ``format`` annotation
        (``date``, ``time``, ``currency``). Unformatted params are cast to str.

        Args:
            name: Template name as defined in the YAML.
            params: Ordered parameter values matching the template variables.

        Returns:
            The rendered body text with all ``{{N}}`` placeholders substituted.

        Raises:
            KeyError: If the template is not registered.
            ValueError: If the number of params doesn't match the template variables.
        """
        tmpl = self._templates[name]
        body_component = next(
            (c for c in tmpl.get("components", []) if c["type"] == "BODY"),
            None,
        )
        if body_component is None:
            return ""

        variables: list[dict] = tmpl.get("variables", []) or body_component.get(
            "variables", []
        )
        if len(params) != len(variables):
            raise ValueError(
                f"Template '{name}' expects {len(variables)} params, got {len(params)}"
            )

        # Build position → formatted string map
        formatted: dict[str, str] = {}
        for var, value in zip(variables, params, strict=True):
            fmt = var.get("format")
            formatter = _FORMATTERS.get(fmt) if fmt else None
            formatted[str(var["position"])] = (
                formatter(value) if formatter else str(value)
            )

        return _PLACEHOLDER_RE.sub(
            lambda m: formatted[m.group(1)], body_component["text"]
        )

    @property
    def names(self) -> list[str]:
        """Return all registered template names."""
        return list(self._templates)
