# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for socialagent init blueprint discovery (beta blueprint set).

Covers:
  - ``socialagent init --list-blueprints`` lists exactly ``minimal``,
    ``personal-assistant``, and ``appointment-scheduler`` — no others.
  - ``socialagent init --blueprint personal-assistant-langgraph`` (a removed blueprint)
    exits with a non-zero status code.
"""

from __future__ import annotations

import argparse

import pytest

from socialagent.cli.blueprints import get_blueprint_dir, list_blueprints
from socialagent.cli.init import cmd_init

# The exact set of blueprints that must ship in the beta.
BETA_BLUEPRINTS = {"minimal", "personal-assistant", "appointment-scheduler"}

# Blueprints removed for the beta — must no longer be listed or usable.
REMOVED_BLUEPRINTS = (
    "personal-assistant-langgraph",
    "multi-party-coordinator",
    "dev-assistant",
)


def _args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with defaults matching the CLI parser."""
    defaults = {"name": None, "list_blueprints": False, "blueprint": None}
    return argparse.Namespace(**{**defaults, **kwargs})


# ── --list-blueprints lists exactly the three beta blueprints ─────────────────────


def test_list_blueprints_returns_exactly_beta_set():
    """list_blueprints() returns exactly the three beta blueprints — no others."""
    assert set(list_blueprints().keys()) == BETA_BLUEPRINTS


def test_list_blueprints_excludes_removed():
    """list_blueprints() does not include any removed blueprint."""
    keys = set(list_blueprints().keys())
    for removed in REMOVED_BLUEPRINTS:
        assert removed not in keys


def test_list_blueprints_command_lists_beta_blueprints(capsys):
    """``socialagent init --list-blueprints`` prints each beta blueprint name."""
    cmd_init(_args(list_blueprints=True))
    out = capsys.readouterr().out
    assert "Available blueprints:" in out
    for name in BETA_BLUEPRINTS:
        assert name in out


def test_list_blueprints_command_omits_removed_blueprints(capsys):
    """``socialagent init --list-blueprints`` does not mention any removed blueprint."""
    cmd_init(_args(list_blueprints=True))
    out = capsys.readouterr().out
    for removed in REMOVED_BLUEPRINTS:
        assert removed not in out


# ── --blueprint <removed> exits non-zero ─────────────────────────────────────────


def test_init_blueprint_langgraph_exits_nonzero(tmp_path, monkeypatch, capsys):
    """``socialagent init --blueprint personal-assistant-langgraph`` exits non-zero."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cmd_init(_args(name="testbot", blueprint="personal-assistant-langgraph"))
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "personal-assistant-langgraph" in err
    # Error should guide the user to the available blueprints.
    assert "Available" in err


@pytest.mark.parametrize("removed", REMOVED_BLUEPRINTS)
def test_init_removed_blueprint_exits_nonzero(removed, tmp_path, monkeypatch):
    """Every removed blueprint name causes ``socialagent init`` to exit non-zero."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cmd_init(_args(name="testbot", blueprint=removed))
    assert exc_info.value.code != 0


def test_get_blueprint_dir_rejects_langgraph():
    """get_blueprint_dir raises ValueError for the removed langgraph blueprint."""
    with pytest.raises(ValueError, match="personal-assistant-langgraph"):
        get_blueprint_dir("personal-assistant-langgraph")


@pytest.mark.parametrize("name", sorted(BETA_BLUEPRINTS))
def test_get_blueprint_dir_accepts_beta_blueprints(name):
    """get_blueprint_dir returns an existing directory for each beta blueprint."""
    bp_dir = get_blueprint_dir(name)
    assert bp_dir.is_dir()
