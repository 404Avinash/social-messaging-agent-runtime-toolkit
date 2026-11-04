# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``socialagent recipe add`` copy behavior outside any project.

The legacy ``socialagent add`` (with its ``.socialagent-manifest.json`` marker)
was removed. ``socialagent recipe add <name>`` copies a recipe's functional
files into the enclosing project (``_project.project_root``: nearest
``agent.py`` / ``socialagent.*.yaml``), falling back to the current working
directory when there is none — which is what these tests exercise — and does
not care what other files happen to be present.

Destinations (per ``recipe_cmd._RECIPE_FILE_DESTS``):
  recipes/<name>/tool.py  -> ./src/tools/<name_with_underscores>.py
  recipes/<name>/skill.md -> ./src/skills/<name_with_underscores>.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from socialagent.cli.recipe_cmd import cmd_recipe_add


def _add(recipe: str) -> None:
    """Invoke ``socialagent recipe add`` for *recipe* via its function signature."""
    cmd_recipe_add(argparse.Namespace(recipe=recipe))


def test_add_copies_files_relative_to_cwd_from_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From an empty cwd, files are copied relative to cwd — no marker needed."""
    monkeypatch.chdir(tmp_path)

    _add("rich-messages")

    assert (tmp_path / "src" / "tools" / "rich_messages.py").is_file()
    # rich-messages ships a skill.md, so it should land too.
    assert (tmp_path / "src" / "skills" / "rich_messages.md").is_file()


def test_add_is_independent_of_unrelated_cwd_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arbitrary unrelated files/dirs in cwd don't change where files land.

    With no ``agent.py`` or ``socialagent.<env>.yaml`` up the tree, copies land
    relative to cwd; unrelated files are never treated as project markers.
    """
    # Populate cwd with arbitrary unrelated content + a nested structure.
    (tmp_path / "random.txt").write_text("not a project marker", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    nested = tmp_path / "deeply" / "nested" / "dir"
    nested.mkdir(parents=True)
    (nested / "leftover.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    _add("voice-reply")

    # Files land under the current tmp cwd regardless of the clutter.
    assert (tmp_path / "src" / "tools" / "voice_reply.py").is_file()
    assert (tmp_path / "src" / "skills" / "voice_reply.md").is_file()
    # And the unrelated content is untouched.
    assert (tmp_path / "random.txt").read_text(
        encoding="utf-8"
    ) == "not a project marker"


def test_add_copied_content_matches_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copied tool.py is a byte-for-byte copy of the recipe source."""
    monkeypatch.chdir(tmp_path)

    _add("generate-image")

    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "recipes" / "generate-image" / "tool.py"
    copied = tmp_path / "src" / "tools" / "generate_image.py"
    assert copied.is_file()
    assert copied.read_bytes() == source.read_bytes()


def test_add_unknown_recipe_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown recipe name exits non-zero and lists available recipes."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _add("does-not-exist")

    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "unknown recipe" in err.lower()
    assert "Available recipes:" in err
    # The real recipe names should be advertised to the user.
    assert "rich-messages" in err

    # Nothing should have been written to cwd for an unknown recipe.
    assert not (tmp_path / "src").exists()


def test_add_empty_recipe_name_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty/missing recipe name exits non-zero with an error message."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _add("")

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "provide a recipe name" in err.lower()
    assert not (tmp_path / "src").exists()
