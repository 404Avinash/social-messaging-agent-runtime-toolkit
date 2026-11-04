# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Blueprint registry for `socialagent init --blueprint <name>`.

Blueprints are template-as-code: complete, runnable project scaffolds that
``socialagent init`` copies verbatim into a new project. They are never imported at
runtime, so the template directories live at the repo root under
``blueprints/`` (alongside ``recipes/``) rather than inside the package. This
module is the only importable piece — it enumerates those directories and
resolves their paths via :mod:`socialagent.cli._assets`, which handles both the
pip-installed and source-checkout layouts.

Each blueprint is a directory under ``blueprints/`` containing:
  - agent.py         — AgentCore Runtime entry point
  - handlers/        — webhook, notification, event, scheduler handlers
  - templates.yaml   — WhatsApp message templates (optional)
  - AGENTS.md        — Blueprint-specific docs (first non-empty,
                       non-heading line is used as the description)

Files are copied as-is into the scaffolded project.
"""

from __future__ import annotations

from pathlib import Path

from socialagent.cli._assets import blueprints_dir

# Packaged blueprints/ when pip-installed, repo-root blueprints/ in a checkout.
BLUEPRINTS_DIR = blueprints_dir()

_BLUEPRINT_NAMES = ["minimal", "personal-assistant", "appointment-scheduler"]

DEFAULT_BLUEPRINT = "minimal"


def _read_description(name: str) -> str:
    """Return the first non-empty, non-heading line of a blueprint's AGENTS.md."""
    agents_md = BLUEPRINTS_DIR / name / "AGENTS.md"
    if not agents_md.is_file():
        return "No description available"
    for line in agents_md.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return "No description available"


def list_blueprints() -> dict[str, str]:
    """Return {name: description} for all available blueprints."""
    return {name: _read_description(name) for name in _BLUEPRINT_NAMES}


def get_blueprint_dir(name: str) -> Path:
    """Return the directory for a blueprint, or raise ValueError."""
    if name not in _BLUEPRINT_NAMES:
        raise ValueError(
            f"Unknown blueprint '{name}'. Available: {', '.join(_BLUEPRINT_NAMES)}"
        )
    return BLUEPRINTS_DIR / name
