# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skill loader — reads markdown files from src/skills/."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_SKILLS_DIR = Path(__file__).parent / "skills"

# The agent chooses the skill name (the load_skill tool passes it through), so
# it is untrusted input to a filesystem path. A bare file stem only: no path
# separators and no dots, so "../../AGENTS" or an absolute path cannot reach a
# markdown file outside src/skills/.
_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _skill_path(name: str) -> Path | None:
    """Return the skill file for ``name`` if it stays inside ``_SKILLS_DIR``."""
    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        return None
    skills_dir = _SKILLS_DIR.resolve()
    # resolve() follows symlinks, so a link inside the directory that points
    # out of it is refused as well.
    path = (skills_dir / f"{name}.md").resolve()
    if path.parent != skills_dir:
        return None
    return path


def load_skill(name: str) -> str:
    """Return the content of a skill markdown file by name."""
    path = _skill_path(name)
    if path is None or not path.is_file():
        available = ", ".join(p.stem for p in sorted(_SKILLS_DIR.glob("*.md")))
        return f"Skill '{name}' not found. Available: {available or 'none'}"
    return path.read_text()


def available_skills_block() -> str:
    """Return a formatted '## Available Skills' section from skill frontmatter."""
    lines = ["## Available Skills"]
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        text = path.read_text()
        if text.startswith("---"):
            _, fm, *_ = text.split("---", 2)
            meta = yaml.safe_load(fm) or {}
        else:
            meta = {}
        name = meta.get("name", path.stem)
        desc = meta.get("description", "")
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)
