# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Local template validation: catch the Meta rejection causes we have actually hit
before a template reaches the provider.

Two severities:

- ``error`` — Meta rejects the template at registration (or the registry cannot
  render it). ``templates sync`` refuses to run while any exist.
- ``warning`` — the template may register but risks reclassification or a
  rejection whose threshold is unknown. Reported, never blocking.

The checks are deliberately limited to rules that are documented in
``docs/templates.md`` or observed against a real WABA. Branding and promotional
tone are not machine-checkable beyond emoji and the project's own brand name,
so passing validation is not a promise of UTILITY approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
# Meta template names: lowercase letters, digits and underscores, max 512.
_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
_BODY_MAX = 1024

# Static characters per variable below which a body is flagged. Observed on one
# WABA: accepted bodies carry 30-75 static characters per variable (the densest
# accepted one, six variables against ~216 characters, is 36); the one rejected
# body had ~25 and also ended on a variable, so the ratio was never isolated.
# This is a heuristic with an unknown true threshold, hence a warning.
MIN_STATIC_CHARS_PER_VARIABLE = 25

# Pictographic ranges that Meta treats as emoji. Punctuation such as the em
# dash and typographic quotes is deliberately outside them.
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, symbols ext.
    "\u2600-\u27bf"  # misc symbols and dingbats
    "\u2b00-\u2bff"  # arrows and stars (e.g. the white star)
    "\ufe0f"  # emoji presentation selector
    "]"
)


@dataclass(frozen=True)
class Finding:
    """One validation result for one template."""

    template: str
    severity: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()}: {self.template}: {self.message}"


def validate_template(
    tmpl: dict[str, Any], prefix: str = "", brand_name: str = ""
) -> list[Finding]:
    """Validate one template definition as it appears in ``templates.yaml``."""
    name = str(tmpl.get("name") or "<unnamed>")
    out: list[Finding] = []

    def err(msg: str) -> None:
        out.append(Finding(name, "error", msg))

    def warn(msg: str) -> None:
        out.append(Finding(name, "warning", msg))

    if not tmpl.get("name"):
        err("missing 'name'")
    else:
        full = f"{prefix}{name}"
        if not _NAME_RE.match(full):
            err(
                f"name {full!r} must use only lowercase letters, digits and "
                "underscores (Meta rejects anything else)"
            )
        if prefix and name.startswith(prefix):
            err(
                f"name already starts with the prefix {prefix!r}; sync adds it "
                f"again and registers {full!r}"
            )
    category = tmpl.get("category")
    if category not in _CATEGORIES:
        err(f"category must be one of {sorted(_CATEGORIES)}, got {category!r}")
    if not tmpl.get("language"):
        err("missing 'language'")

    bodies = [c for c in tmpl.get("components") or [] if c.get("type") == "BODY"]
    if len(bodies) != 1:
        err(f"needs exactly one BODY component, found {len(bodies)}")
        return out
    body = bodies[0]
    text = str(body.get("text") or "")
    stripped = text.strip()
    if not stripped:
        err("BODY text is empty")
        return out
    if len(text) > _BODY_MAX:
        err(f"BODY is {len(text)} characters; Meta allows {_BODY_MAX}")

    positions = [int(p) for p in _PLACEHOLDER_RE.findall(text)]
    distinct = sorted(set(positions))
    n = len(distinct)
    if distinct and distinct != list(range(1, n + 1)):
        err(
            f"placeholders must be {{{{1}}}}..{{{{{n}}}}} in sequence, found {distinct}"
        )

    if _PLACEHOLDER_RE.match(stripped):
        err("BODY opens on a variable; Meta rejects that — put static text first")
    if re.search(r"\{\{\d+\}\}$", stripped):
        err("BODY closes on a variable; Meta rejects that — put static text last")

    # The registry renders from `variables` (top level, else on the BODY), so a
    # count mismatch makes every send of this template raise.
    variables = tmpl.get("variables") or body.get("variables") or []
    if n and len(variables) != n:
        err(f"BODY has {n} placeholder(s) but 'variables' lists {len(variables)}")
    elif variables and sorted(int(v.get("position", 0)) for v in variables) != (
        distinct
    ):
        err("'variables' positions do not match the BODY placeholders")

    example = (body.get("example") or {}).get("body_text")
    if n and not example:
        err("BODY has variables but no example.body_text; Meta requires samples")
    elif example:
        row = example[0] if isinstance(example, list) and example else []
        if not isinstance(row, list) or len(row) != n:
            err(f"example.body_text needs one row of {n} sample(s), got {row!r}")

    if n:
        static = len(_PLACEHOLDER_RE.sub("", stripped).strip())
        per_var = static / n
        if per_var < MIN_STATIC_CHARS_PER_VARIABLE:
            warn(
                f"{n} variable(s) against {static} characters of static text "
                f"(~{per_var:.0f} per variable); Meta may reject it as 'too "
                "many variables for its length'. Add surrounding text."
            )

    if category == "UTILITY":
        emoji = sorted(set(_EMOJI_RE.findall(text)) - {"\ufe0f"})
        if emoji:
            warn(
                f"UTILITY template contains emoji {''.join(emoji)}; Meta tends to "
                "reclassify these as MARKETING"
            )
        if brand_name and brand_name.lower() in text.lower():
            warn(
                f"UTILITY template names the brand {brand_name!r}; branding risks "
                "reclassification as MARKETING"
            )
    return out


def validate_file(
    path: str | Path,
    prefix: str = "",
    brand_name: str = "",
    names: list[str] | None = None,
) -> list[Finding]:
    """Validate the templates in a ``templates.yaml``.

    *names* limits the findings to those templates (YAML names, unprefixed);
    entries that are not mappings are always reported. ``None`` means all.

    Raises ``ValueError`` when the file is not a templates document at all.
    """
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with a 'templates' list")
    templates = data.get("templates") or []
    if not isinstance(templates, list):
        raise ValueError(f"{path}: 'templates' must be a list")

    findings: list[Finding] = []
    seen: set[str] = set()
    for tmpl in templates:
        if not isinstance(tmpl, dict):
            findings.append(Finding("<entry>", "error", f"not a mapping: {tmpl!r}"))
            continue
        # TemplateRegistry indexes by name alone, so a second entry with the
        # same name (even in another language) silently replaces the first.
        tname = str(tmpl.get("name"))
        if tname in seen:
            findings.append(
                Finding(tname, "error", "duplicate name; the registry keeps only one")
            )
        seen.add(tname)
        findings.extend(validate_template(tmpl, prefix=prefix, brand_name=brand_name))
    if names is not None:
        wanted = set(names)
        findings = [
            f for f in findings if f.template in wanted or f.template == "<entry>"
        ]
    return findings


def has_errors(findings: list[Finding]) -> bool:
    """Whether any finding blocks a sync."""
    return any(f.severity == "error" for f in findings)
