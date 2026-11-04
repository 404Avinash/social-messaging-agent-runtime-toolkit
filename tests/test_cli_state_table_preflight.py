# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Code that keeps per-user state needs the state table; the deploy says so.

With ``features.state_table`` off, the agent's ``socialagent_runtime.state``
writes to a file inside the runtime session's container, gone when the session
ends, and the Lambda-side ``ctx.state`` reads ``None`` and writes nothing. The
deploy succeeds either way, and the only other sign is a start-up WARNING in
CloudWatch. The blueprints ship the flag on in
their dev YAML, and ``socialagent config init --env <env>`` copies an existing
environment's ``features`` into the file it writes, but a file written by hand
or by an earlier CLI, and an existing project's YAML, can leave it off.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from socialagent.cli import _deploy
from socialagent.cli._deploy import state_calls_without_table

_AGENT = "from socialagent_runtime import state\nstate.put('p', 'k', {})\n"
_HANDLER = "async def h(data, ctx):\n    await ctx.state.get(data['phone'], 'k')\n"


def _project(tmp_path: Path, flag=None) -> tuple[dict, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "agent.py").write_text(_AGENT)
    config: dict = {"project": "p", "environment": "dev", "region": "us-east-1"}
    if flag is not None:
        config["features"] = {"state_table": flag}
    return config, tmp_path


@pytest.mark.parametrize("flag", [None, False])
def test_state_calls_with_the_flag_off_are_reported(tmp_path, flag):
    config, d = _project(tmp_path, flag)
    (d / "handlers").mkdir()
    (d / "handlers" / "event.py").write_text(_HANDLER)
    (d / "src" / "tools").mkdir(parents=True)
    (d / "src" / "tools" / "__init__.py").write_text(_AGENT)
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "handlers/event.py:2",
        "src/tools/__init__.py:2",
    ]


def test_flag_on_is_fine(tmp_path):
    config, d = _project(tmp_path, True)
    assert state_calls_without_table(config, d) == []


def test_a_quoted_true_in_the_yaml_counts_as_on(tmp_path):
    config, d = _project(tmp_path, "true")
    assert state_calls_without_table(config, d) == []


def test_no_state_calls_is_fine_with_the_flag_off(tmp_path):
    config, d = _project(tmp_path, False)
    (d / "agent.py").write_text("print('stateless')\n")
    assert state_calls_without_table(config, d) == []


def test_code_that_is_not_the_projects_own_is_skipped(tmp_path):
    """The vendored runtime, virtualenvs, installed packages, hidden directories,
    tests and Terraform all call state too, and none of it is the project
    keeping state."""
    config, d = _project(tmp_path)
    for skipped in (
        "socialagent_runtime",
        ".venv/lib/python3.12/site-packages/x",
        "venv/lib",
        "src/__pycache__",
        ".socialagent/runtime-backups/1",
        "node_modules/x",
        "vendor/lib/python3.12/site-packages/x",
        "tests",
        "test",
        ".aws-sam/build/Fn",
        "infra-terraform",
        "infrastructure",
    ):
        (d / skipped).mkdir(parents=True)
        (d / skipped / "m.py").write_text(_AGENT)
    assert state_calls_without_table(config, d) == ["agent.py:2"]


def test_build_output_and_a_plain_env_directory_ship_and_are_read(tmp_path):
    """The agent archive packs build/ and a directory named env/ that is not a
    virtualenv, so the code there runs in the image."""
    config, d = _project(tmp_path)
    for shipped in ("build/lib", "env/lib"):
        (d / shipped).mkdir(parents=True)
        (d / shipped / "m.py").write_text(_AGENT)
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "build/lib/m.py:2",
        "env/lib/m.py:2",
    ]


def test_a_virtualenv_is_skipped_whatever_its_name(tmp_path):
    config, d = _project(tmp_path)
    (d / "pyenv-3.12" / "lib").mkdir(parents=True)
    (d / "pyenv-3.12" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (d / "pyenv-3.12" / "lib" / "m.py").write_text(_AGENT)
    assert state_calls_without_table(config, d) == ["agent.py:2"]


def test_a_package_named_like_a_skipped_directory_is_read_below_the_top(tmp_path):
    """tests/ and infrastructure/ are skipped at the top only: a project may
    ship src/infrastructure as its own code."""
    config, d = _project(tmp_path)
    for package in ("src/infrastructure", "src/test"):
        (d / package).mkdir(parents=True)
        (d / package / "store.py").write_text(_AGENT)
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "src/infrastructure/store.py:2",
        "src/test/store.py:2",
    ]


def test_a_file_that_does_not_parse_or_decode_is_skipped(tmp_path):
    config, d = _project(tmp_path)
    (d / "broken.py").write_text("def (:\n")
    (d / "latin1.py").write_bytes(('x = "caf\xe9"\n' + _AGENT).encode("latin-1"))
    assert state_calls_without_table(config, d) == ["agent.py:2"]


def test_a_declared_source_encoding_is_honoured(tmp_path):
    config, d = _project(tmp_path)
    (d / "agent.py").write_bytes(
        ('# -*- coding: latin-1 -*-\nx = "caf\xe9"\n' + _AGENT).encode("latin-1")
    )
    assert state_calls_without_table(config, d) == ["agent.py:4"]


def test_a_file_that_cannot_be_read_is_skipped(tmp_path):
    """An Emacs lock file is a dangling symlink named .#<file>; a deploy run
    while a buffer has unsaved edits must not fail on it."""
    config, d = _project(tmp_path)
    (d / "handlers").mkdir()
    os.symlink("user@host.123:456", d / ".#agent.py")
    os.symlink("user@host.123:456", d / "handlers" / ".#scheduler.py")
    os.symlink(d / "missing.py", d / "gone.py")
    assert state_calls_without_table(config, d) == ["agent.py:2"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 0 file")
def test_an_unreadable_file_is_skipped(tmp_path):
    config, d = _project(tmp_path)
    locked = d / "locked.py"
    locked.write_text(_AGENT)
    locked.chmod(0)
    try:
        assert state_calls_without_table(config, d) == ["agent.py:2"]
    finally:
        locked.chmod(0o600)


def test_a_file_too_deep_to_parse_is_skipped(tmp_path, monkeypatch):
    config, d = _project(tmp_path)
    (d / "generated.py").write_text(_AGENT)
    parse = _deploy.ast.parse

    def fake_parse(source, filename="<unknown>", *a, **kw):
        if filename.endswith("generated.py"):
            raise RecursionError("maximum recursion depth exceeded")
        return parse(source, filename, *a, **kw)

    monkeypatch.setattr(_deploy.ast, "parse", fake_parse)
    assert state_calls_without_table(config, d) == ["agent.py:2"]


def test_a_syntax_warning_in_the_project_is_not_printed(tmp_path):
    config, d = _project(tmp_path)
    (d / "agent.py").write_text('pattern = "\\d+"\n' + _AGENT)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert state_calls_without_table(config, d) == ["agent.py:3"]
    assert not [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_symlinked_directories_are_followed_once(tmp_path):
    """The agent archive ships what a symlinked directory holds; a link loop
    must still end."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "m.py").write_text(_AGENT)
    config, d = _project(tmp_path / "p")
    (d / "src").mkdir()
    os.symlink(shared, d / "src" / "lib")
    os.symlink(d / "src", d / "src" / "loop")
    assert state_calls_without_table(config, d) == ["agent.py:2", "src/lib/m.py:2"]


def test_a_source_dir_outside_the_project_is_what_is_read(tmp_path):
    """The image is built from source_dir alone, so the project's own agent.py
    does not deploy."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "m.py").write_text(_AGENT)
    config, d = _project(tmp_path / "p")
    config["agent"] = {"source_dir": "../elsewhere"}
    assert state_calls_without_table(config, d) == ["../elsewhere/m.py:2"]


def test_a_handlers_dir_outside_the_project_is_read(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "event.py").write_text(_HANDLER)
    config, d = _project(tmp_path / "p")
    config["agent"] = {"handlers_dir": "../elsewhere"}
    assert state_calls_without_table(config, d) == [
        "../elsewhere/event.py:2",
        "agent.py:2",
    ]


@pytest.mark.parametrize(
    "source", ["build/agent", "env/agent", "infrastructure/agent", "test/agent"]
)
def test_a_source_dir_under_a_skipped_name_is_read(tmp_path, source):
    """A source_dir under a name the project's top skips is what the image is
    built from; the source's own top-level tests/ is still skipped."""
    config, d = _project(tmp_path)
    (d / source / "tests").mkdir(parents=True)
    (d / source / "agent.py").write_text(_AGENT)
    (d / source / "tests" / "test_agent.py").write_text(_AGENT)
    config["agent"] = {"source_dir": source}
    assert state_calls_without_table(config, d) == [f"{source}/agent.py:2"]


def test_only_the_handler_files_the_lambda_zips_carry_are_read(tmp_path):
    """A Lambda zip holds its handler file and handlers/shared/ when that is a
    package; another module in handlers/ ships nowhere."""
    config, d = _project(tmp_path)
    handlers = d / "handlers"
    (handlers / "shared").mkdir(parents=True)
    (handlers / "event.py").write_text(_HANDLER)
    (handlers / "helpers.py").write_text(_AGENT)
    (handlers / "shared" / "store.py").write_text(_HANDLER)
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "handlers/event.py:2",
    ]
    (handlers / "shared" / "__init__.py").write_text("")
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "handlers/event.py:2",
        "handlers/shared/store.py:2",
    ]


@pytest.mark.parametrize("scheduler_lambda", [True, False])
def test_scheduler_py_is_read_only_when_its_lambda_is_built(tmp_path, scheduler_lambda):
    config, d = _project(tmp_path)
    config["features"] = {"scheduler_lambda": scheduler_lambda}
    (d / "handlers").mkdir()
    (d / "handlers" / "scheduler.py").write_text(_HANDLER)
    expected = ["handlers/scheduler.py:2"] if scheduler_lambda else []
    assert state_calls_without_table(config, d) == ["agent.py:2", *expected]


def test_a_handlers_dir_inside_the_agent_source_ships_in_the_image_too(tmp_path):
    """The agent archive leaves out only a top-level handlers/; a handlers_dir
    with another name is in the image, so its other modules are agent code."""
    config, d = _project(tmp_path)
    (d / "app").mkdir()
    (d / "app" / "webhook.py").write_text(_HANDLER)
    (d / "app" / "tools.py").write_text(_AGENT)
    config["agent"] = {"handlers_dir": "app"}
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "app/tools.py:2",
        "app/webhook.py:2",
    ]


def test_a_file_reached_twice_is_listed_once(tmp_path):
    config, d = _project(tmp_path)
    (d / "src").mkdir()
    os.symlink(d / "agent.py", d / "src" / "alias.py")
    assert state_calls_without_table(config, d) == ["agent.py:2"]


def test_paths_given_through_a_symlink_print_relative_to_the_project(tmp_path):
    """On macOS /var is a link to /private/var: a YAML path written one way
    and a project found the other must still print as src/m.py."""
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    config, d = _project(tmp_path / "real" / "p")
    (d / "src").mkdir()
    (d / "src" / "m.py").write_text(_AGENT)
    config["agent"] = {"source_dir": str(tmp_path / "link" / "p" / "src")}
    assert state_calls_without_table(config, d) == ["src/m.py:2"]


def test_ctx_outside_the_handlers_is_not_the_handler_context(tmp_path):
    """A click command's ``ctx.state`` is not per-user state; in a handler file
    ``ctx`` is the handler context."""
    config, d = _project(tmp_path)
    (d / "cli.py").write_text(
        "import click\n\n\n@click.pass_context\ndef cmd(ctx):\n    ctx.state.get('k')\n"
    )
    (d / "handlers").mkdir()
    (d / "handlers" / "event.py").write_text(_HANDLER)
    assert state_calls_without_table(config, d) == ["agent.py:2", "handlers/event.py:2"]


def test_two_calls_on_one_line_are_listed_once(tmp_path):
    config, d = _project(tmp_path)
    (d / "agent.py").write_text(
        "from socialagent_runtime import state\n"
        "state.put('p', 'a', 1); state.put('p', 'b', 2)\n"
    )
    assert state_calls_without_table(config, d) == ["agent.py:2"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 0 directory")
@pytest.mark.parametrize("mode", [0o000, 0o444])
def test_a_directory_that_cannot_be_read_is_skipped(tmp_path, mode):
    """A Docker volume can be mode 000; a directory that can be listed but not
    entered (0444) fails a stat on what it holds. Neither may stop the deploy."""
    config, d = _project(tmp_path)
    data = d / "data"
    (data / "inner").mkdir(parents=True)
    (data / "m.py").write_text(_AGENT)
    data.chmod(mode)
    try:
        assert state_calls_without_table(config, d) == ["agent.py:2"]
    finally:
        data.chmod(0o700)


def test_an_empty_source_or_handlers_dir_means_the_default(tmp_path):
    """An empty ``source_dir:`` or ``handlers_dir:`` loads as None; the tfvars
    read it as the default, and so does the scan."""
    config, d = _project(tmp_path)
    (d / "handlers").mkdir()
    (d / "handlers" / "event.py").write_text(_HANDLER)
    config["agent"] = {"source_dir": None, "handlers_dir": None}
    assert state_calls_without_table(config, d) == [
        "agent.py:2",
        "handlers/event.py:2",
    ]


def test_a_source_dir_that_holds_the_project_reads_it_once(tmp_path):
    """``source_dir: ..`` builds the parent into the image: the parent's code
    is read, and the project inside it once, with its own top-level skips."""
    config, d = _project(tmp_path / "p")
    (d / "tests").mkdir()
    (d / "tests" / "test_agent.py").write_text(_AGENT)
    (tmp_path / "main.py").write_text(_AGENT)
    config["agent"] = {"source_dir": ".."}
    assert state_calls_without_table(config, d) == ["../main.py:2", "agent.py:2"]


def test_deploy_warns_but_does_not_block(tmp_path, capsys):
    config, d = _project(tmp_path)
    _deploy.warn_if_state_calls_without_table(config, d)
    out = capsys.readouterr().out
    assert "agent.py:2" in out and "features.state_table" in out


def test_the_warning_lists_five_calls_and_counts_the_rest(tmp_path, capsys):
    config, d = _project(tmp_path)
    (d / "agent.py").write_text(
        "from socialagent_runtime import state\n" + "state.get('p', 'k')\n" * 7
    )
    _deploy.warn_if_state_calls_without_table(config, d)
    out = capsys.readouterr().out
    assert "agent.py:6" in out and "agent.py:7" not in out and "and 2 more" in out


def test_nothing_is_printed_with_the_flag_on(tmp_path, capsys):
    config, d = _project(tmp_path, True)
    _deploy.warn_if_state_calls_without_table(config, d)
    assert capsys.readouterr().out == ""
