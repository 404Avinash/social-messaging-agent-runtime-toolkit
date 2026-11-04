# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``socialagent recipe`` command group — add and list recipes.

Recipes are self-contained, copyable bundles living under the top-level
``recipes/<name>/`` directory at the SMART repo root. ``socialagent recipe add``
copies a recipe's functional files (``tool.py``, ``skill.md``) into the
enclosing project; ``socialagent recipe list`` enumerates available recipes
with a one-line description read from each recipe's ``README.md``.

The command writes no manifest and reads no recipe metadata: it copies files
into the enclosing project (``_project.project_root``: the nearest directory
with a ``socialagent.<env>.yaml`` or ``agent.py``, else ``cwd``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from socialagent.cli._assets import recipes_dir
from socialagent.cli._project import project_root

# Each recipe file is copied to a fixed destination relative to the project
# root (``_project.project_root``). The destination filename is the
# recipe directory name with hyphens turned into underscores: the copied tool.py
# is imported by the developer's framework wrapper (`from src.tools.rich_messages
# import ...`), and a hyphen is never legal in a Python import path. These are
# also the names the personal-assistant blueprint pre-wires, so the
# skip-if-exists guard sees a pre-wired copy instead of adding a dead duplicate.
#   tool.py  -> src/tools/<recipe_name>.py
#   skill.md -> src/skills/<recipe_name>.md
_RECIPE_FILE_DESTS: dict[str, tuple[str, str]] = {
    "tool.py": ("src/tools", ".py"),
    "skill.md": ("src/skills", ".md"),
}


def _find_recipes_dir() -> Path:
    """Resolve the ``recipes/`` directory.

    Delegates to :func:`socialagent.cli._assets.recipes_dir`, which prefers the copy
    packaged into the wheel and falls back to ``<repo_root>/recipes/`` in a
    source checkout. Resolved from the installed location rather than cwd so the
    lookup is independent of where the command is invoked.
    """
    return recipes_dir()


def _available_recipes(recipes_dir: Path) -> list[str]:
    """Return sorted names of recipe subdirectories, or [] if none/unreadable."""
    try:
        return sorted(p.name for p in recipes_dir.iterdir() if p.is_dir())
    except OSError:
        return []


def _first_description_line(readme: Path) -> str:
    """Return the first non-empty, non-heading line from *readme*.

    Falls back to the placeholder string when the file has no suitable line.
    """
    try:
        for line in readme.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
    except OSError:
        pass
    return "No description available"


def _recipe_dest(recipe_name: str, source_file: str) -> Path:
    """Map a recipe source filename to its destination path relative to the project."""
    subdir, suffix = _RECIPE_FILE_DESTS[source_file]
    return Path(subdir) / f"{recipe_name.replace('-', '_')}{suffix}"


def cmd_recipe_add(args: argparse.Namespace) -> None:
    """``socialagent recipe add <name>`` — copy a recipe's files into the project."""
    recipes_dir = _find_recipes_dir()
    available = _available_recipes(recipes_dir)

    recipe_name = getattr(args, "recipe", None)
    if not recipe_name:
        print("ERROR: provide a recipe name.", file=sys.stderr)
        _print_available(available)
        sys.exit(1)

    recipe_dir = recipes_dir / recipe_name
    if not recipe_dir.is_dir():
        print(f"ERROR: unknown recipe '{recipe_name}'.", file=sys.stderr)
        _print_available(available)
        sys.exit(1)

    # The enclosing project (nearest socialagent.*.yaml / agent.py), so running
    # from src/tools/ does not create src/tools/src/tools/. Falls back to cwd.
    cwd = project_root()
    if cwd != Path.cwd().resolve():
        print(f"Project: {cwd}")
    for source_file in _RECIPE_FILE_DESTS:
        src_path = recipe_dir / source_file
        if not src_path.is_file():
            # skill.md (and, in principle, tool.py) may legitimately be absent.
            continue

        dest_rel = _recipe_dest(recipe_name, source_file)
        dest_path = cwd / dest_rel

        if dest_path.exists():
            print(f"! {dest_rel} (skipped — already exists)")
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(src_path.read_bytes())
        print(f"✓ {dest_rel}")

    # Absolute path: a relative `recipes/<name>/README.md` exists only in the
    # SMART checkout, never in the developer's project.
    print(f"\nWiring instructions: {recipe_dir / 'README.md'}")


def cmd_recipe_list(args: argparse.Namespace) -> None:
    """``socialagent recipe list`` — list available recipes with descriptions."""
    recipes_dir = _find_recipes_dir()
    if not recipes_dir.is_dir():
        print("ERROR: recipes/ directory not found", file=sys.stderr)
        sys.exit(1)

    try:
        entries = sorted(p for p in recipes_dir.iterdir() if p.is_dir())
    except OSError as exc:
        print(f"ERROR: could not read recipes/ directory: {exc}", file=sys.stderr)
        sys.exit(1)

    for recipe_dir in entries:
        readme = recipe_dir / "README.md"
        if readme.is_file():
            description = _first_description_line(readme)
        else:
            description = "No description available"
        print(f"  {recipe_dir.name:<20} {description}")


def _print_available(available: list[str]) -> None:
    """Print the list of available recipe names to stderr."""
    if available:
        print("Available recipes:", file=sys.stderr)
        for name in available:
            print(f"  - {name}", file=sys.stderr)
    else:
        print("No recipes available.", file=sys.stderr)
