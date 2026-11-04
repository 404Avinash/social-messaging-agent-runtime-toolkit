# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``socialagent recipe`` command group (``recipe add`` / ``recipe list``).

These tests exercise the recipe CLI in ``src/socialagent/cli/recipe_cmd.py``:

- ``socialagent recipe add <name>`` copies a recipe's functional files (``tool.py``,
  ``skill.md``) into ``cwd`` under ``src/tools/`` and ``src/skills/``, never
  overwriting an existing destination and never writing ``.socialagent-manifest.json``.
- ``socialagent recipe list`` enumerates available recipes with a one-line description
  read from each recipe's ``README.md`` (placeholder when absent).

All tests run offline. ``cmd_recipe_add`` resolves the recipe source from the
REAL repo ``recipes/`` dir (via ``_find_recipes_dir`` traversal from the CLI
source path), so ``monkeypatch.chdir(tmp_path)`` is used to keep copies in tmp.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from socialagent.cli import recipe_cmd
from socialagent.cli.recipe_cmd import (
    _find_recipes_dir,
    cmd_recipe_add,
    cmd_recipe_list,
)

# Recipes that exist in the real repo recipes/ directory after relocation.
_REAL_RECIPES = ("rich-messages", "voice-reply", "generate-image")


@pytest.fixture
def in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Change cwd to an isolated temp directory and return it."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── socialagent recipe add — copy behavior ────────────────────────────────────────────


def test_recipe_add_copies_tool_to_destination(in_tmp_cwd: Path) -> None:
    """recipe add copies tool.py to src/tools/<name>.py relative to cwd."""
    cmd_recipe_add(argparse.Namespace(recipe="rich-messages"))

    dest = in_tmp_cwd / "src" / "tools" / "rich_messages.py"
    assert dest.is_file()

    source = _find_recipes_dir() / "rich-messages" / "tool.py"
    assert dest.read_bytes() == source.read_bytes()


def test_recipe_add_copies_skill_when_present(in_tmp_cwd: Path) -> None:
    """recipe add also copies skill.md to src/skills/<name>.md when present."""
    cmd_recipe_add(argparse.Namespace(recipe="rich-messages"))

    skill_src = _find_recipes_dir() / "rich-messages" / "skill.md"
    if not skill_src.is_file():
        pytest.skip("rich-messages recipe has no skill.md")

    dest = in_tmp_cwd / "src" / "skills" / "rich_messages.md"
    assert dest.is_file()
    assert dest.read_bytes() == skill_src.read_bytes()


@pytest.mark.usefixtures("in_tmp_cwd")
def test_recipe_add_no_warning_when_no_conflict(capsys) -> None:
    """recipe add copies cleanly and prints no 'skipped' warning when no conflict."""
    cmd_recipe_add(argparse.Namespace(recipe="rich-messages"))

    out = capsys.readouterr().out
    assert "skipped" not in out
    assert "✓" in out  # at least one file copied


# ── socialagent recipe add — skip behavior ────────────────────────────────────────────


def test_recipe_add_skips_existing_file_without_overwriting(
    in_tmp_cwd: Path, capsys
) -> None:
    """An existing destination file is left unchanged and reported as skipped."""
    dest = in_tmp_cwd / "src" / "tools" / "rich_messages.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# SENTINEL — user-modified content, must not be overwritten\n"
    dest.write_text(sentinel, encoding="utf-8")

    cmd_recipe_add(argparse.Namespace(recipe="rich-messages"))

    # File content is preserved exactly.
    assert dest.read_text(encoding="utf-8") == sentinel
    # And stdout warns that it was skipped, mentioning the path.
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "rich_messages.py" in out


# ── socialagent recipe add — no manifest guarantee ────────────────────────────────────


def test_recipe_add_writes_no_manifest_anywhere(in_tmp_cwd: Path) -> None:
    """recipe add never writes a .socialagent-manifest.json anywhere under cwd."""
    cmd_recipe_add(argparse.Namespace(recipe="rich-messages"))

    manifests = list(in_tmp_cwd.rglob(".socialagent-manifest.json"))
    assert manifests == [], f"unexpected manifest(s) written: {manifests}"


# ── socialagent recipe add — unknown recipe error ─────────────────────────────────────


@pytest.mark.usefixtures("in_tmp_cwd")
def test_recipe_add_unknown_recipe_exits_nonzero(capsys) -> None:
    """An unknown recipe name exits non-zero with available recipes on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        cmd_recipe_add(argparse.Namespace(recipe="nope-not-a-recipe"))

    # Non-zero exit code.
    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "nope-not-a-recipe" in err
    assert "Available recipes" in err
    # The real recipes are listed so the developer can pick a valid one.
    for name in _REAL_RECIPES:
        assert name in err


# ── socialagent recipe list — output ──────────────────────────────────────────────────


def test_recipe_list_prints_line_per_recipe(capsys) -> None:
    """recipe list prints at least one line per recipe in recipes/."""
    cmd_recipe_list(argparse.Namespace())

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]

    # Every real recipe appears in the output.
    for name in _REAL_RECIPES:
        assert any(name in line for line in lines), f"{name} missing from list output"

    # At least one printed line per recipe directory.
    recipe_dirs = [p for p in _find_recipes_dir().iterdir() if p.is_dir()]
    assert len(lines) >= len(recipe_dirs)


# ── socialagent recipe list — placeholder for missing README ──────────────────────────


def test_recipe_list_placeholder_when_no_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A recipe directory lacking README.md is listed with a placeholder."""
    # Build a synthetic recipes/ dir: one recipe with tool.py but NO README.md.
    fake_recipes = tmp_path / "recipes"
    no_readme = fake_recipes / "no-readme-recipe"
    no_readme.mkdir(parents=True)
    (no_readme / "tool.py").write_text("# tool\n", encoding="utf-8")

    monkeypatch.setattr(recipe_cmd, "_find_recipes_dir", lambda: fake_recipes)

    cmd_recipe_list(argparse.Namespace())

    out = capsys.readouterr().out
    assert "no-readme-recipe" in out
    assert "No description available" in out
