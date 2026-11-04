# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Skill loader for this project — reads markdown files from src/skills/.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# The index is built on every turn, so each refused operation is logged once.
_warned_refused: set[str] = set()


@dataclass
class Skill:
    name: str
    description: str
    content: str
    operations: list[dict]  # [{name, description, tool}]
    auto_include: bool = False


def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    if yaml is None:
        return {}, parts[2].strip()
    try:
        return yaml.safe_load(parts[1]) or {}, parts[2].strip()
    except yaml.YAMLError:
        return {}, text


def _load_all() -> dict[str, Skill]:
    d = Path(__file__).parent / "skills"
    if not d.is_dir():
        return {}
    return {
        (meta := _parse(f.read_text()))[0].get("name", f.stem): Skill(
            name=meta[0].get("name", f.stem),
            description=meta[0].get("description", ""),
            content=meta[1],
            operations=meta[0].get("operations", []),
            auto_include=meta[0].get("auto_include", False),
        )
        for f in sorted(d.glob("*.md"))
    }


def load_skill_index(accepted: Collection[str] | None = None) -> str:
    """The skills section of the system prompt.

    ``accepted`` names the operations ``schedule_recurring`` creates (agent.py
    passes ``RECURRING_OPERATIONS``). A skill that declares any other one for
    that tool has it left out of the prompt, with a WARNING, as the prompt
    tells the model that every operation listed works. ``None`` lists every
    declared operation.
    """
    skills = _load_all()
    if not skills:
        return ""
    lines = ["## Available Skills\n"]
    auto_included = []
    for s in skills.values():
        lines.append(f"- **{s.name}**: {s.description}")
        if s.auto_include:
            auto_included.append(s)
    # Auto-include critical skill content in the prompt
    if auto_included:
        lines.append("\n## Skill Instructions (auto-loaded)\n")
        for s in auto_included:
            lines.append(s.content)
            lines.append("")
    # Build operations index from skills
    ops = []
    for s in skills.values():
        for op in s.operations:
            if not isinstance(op, dict) or not op.get("name"):
                continue
            tool = op.get("tool", "schedule_recurring")
            if (
                accepted is not None
                and tool == "schedule_recurring"
                and op["name"] not in accepted
            ):
                if op["name"] not in _warned_refused:
                    _warned_refused.add(op["name"])
                    logger.warning(
                        "Skill %r declares scheduler operation %r, which "
                        "schedule_recurring does not accept, so it is left out "
                        "of the prompt. Write its @app.on_schedule handler in "
                        "handlers/scheduler.py and add it to RECURRING_OPERATIONS "
                        "in src/tools/__init__.py.",
                        s.name,
                        op["name"],
                    )
                continue
            ops.append(
                f"- `{op['name']}`: {op.get('description', '')} (from skill: {s.name}, tool: {tool})"
            )
    if ops:
        lines.append(
            "\n## Registered Scheduler Operations\nOnly use these operation names with schedule_recurring:\n"
        )
        lines.extend(ops)
        lines.append(
            "\nDo NOT invent custom operation names — only the ones listed above will work."
        )
    return "\n".join(lines)


def load_skill(name: str) -> str:
    skills = _load_all()
    if name not in skills:
        return f"Skill '{name}' not found. Available: {', '.join(skills) or 'none'}"
    return skills[name].content
