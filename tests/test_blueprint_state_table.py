# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A blueprint that keeps per-user state turns on the state table.

``features.state_table`` (``enable_state_table``, default false in
``infra-terraform/variables.tf``) is easy to leave off, and a missing flag fails
without an error: the agent's
``socialagent_runtime.state`` writes to a file inside the runtime container,
which is gone when the session ends, and the Lambda-side ``ctx.state`` reads
nothing. A booking or a remembered fact then lasts one session, and the
per-user rate limit is not enforced.

The detector is the one ``socialagent deploy`` warns with
(``socialagent.cli._deploy.state_calls``), so the two cannot drift. It reads
the code with ``ast``, so a commented-out example does not count as a state
call.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import pytest
import yaml

from socialagent.cli._deploy import state_call_lines, state_calls
from socialagent.cli._tfvars import generate
from socialagent.cli.blueprints import _BLUEPRINT_NAMES, get_blueprint_dir
from socialagent.cli.config_file import load
from socialagent.cli.init import cmd_init

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(blueprint: str) -> dict:
    return yaml.safe_load(
        (get_blueprint_dir(blueprint) / "socialagent.dev.yaml").read_text(
            encoding="utf-8"
        )
    )


def _state_calls(blueprint: str) -> list[str]:
    """``file:line`` of every per-user state call the blueprint makes."""
    return state_calls(_config(blueprint), get_blueprint_dir(blueprint), strict=True)


@pytest.mark.parametrize("blueprint", _BLUEPRINT_NAMES)
def test_state_users_get_a_state_table(blueprint: str) -> None:
    calls = _state_calls(blueprint)
    if not calls:
        pytest.skip(f"{blueprint} keeps no per-user state")
    assert (_config(blueprint).get("features") or {}).get("state_table") is True, (
        f"{blueprint} reads or writes per-user state ({', '.join(calls)}), but "
        "its socialagent.dev.yaml leaves features.state_table off, so nothing "
        "it stores survives the session"
    )


def test_the_check_sees_what_the_blueprints_do() -> None:
    """Pins the detector, so a refactor that blinds it fails here instead of
    turning the test above into skips."""
    blind = "the state-call detector no longer sees this blueprint's state calls"
    assert any(c.startswith("src/") for c in _state_calls("personal-assistant")), blind
    calls = _state_calls("appointment-scheduler")
    assert any(c.startswith("src/") for c in calls), blind
    assert any(c.startswith("handlers/") for c in calls), blind
    assert _state_calls("minimal") == [], (
        "minimal now keeps per-user state: turn its state table on, and update "
        "the lines that name the blueprints with the table on: A021 and M2 in "
        "docs/threat-model.md, README.md, docs/configuration.md, AGENTS.md, "
        "SECURITY.md and docs/state.md"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from socialagent_runtime import state\nstate.put('p', 'k', {})\n",
        "from socialagent_runtime import state as st\nst.put('p', 'k', {})\n",
        "import socialagent_runtime.state as state\nstate.put('p', 'k', {})\n",
        "import socialagent_runtime.state\nsocialagent_runtime.state.put('p', 'k', {})\n",
        "import socialagent_runtime\nsocialagent_runtime.state.put('p', 'k', {})\n",
        "import socialagent_runtime as sr\nsr.state.put('p', 'k', {})\n",
        "import socialagent_runtime.notify\nsocialagent_runtime.state.put('p', 'k', {})\n",
        "from socialagent_runtime.state import put\nput('p', 'k', {})\n",
        "from socialagent_runtime.state import put as save\nsave('p', 'k', {})\n",
        "from socialagent_runtime.state import *\nclaim_once('p', 'k')\n",
    ],
)
def test_a_state_call_counts_however_state_is_imported(source: str) -> None:
    assert state_call_lines(ast.parse(source)) == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from socialagent_runtime import state\n# state.put('p', 'k', {})\n",
        "import socialagent_runtime\nsocialagent_runtime.notify.send_message('p', {})\n",
        "state = {}\nstate.get('k')\n",
        "import socialagent_runtime.notify as n\nn.state.put('p', 'k', {})\n",
        "app = BedrockAgentCoreApp()\napp.state.get('k')\n",
    ],
)
def test_other_calls_do_not_count(source: str) -> None:
    assert state_call_lines(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [
        "from socialagent import SocialAgentApp\napp = SocialAgentApp(config)\n"
        "async def hook(msg):\n    await app.state.get('p', 'k')\n",
        "import socialagent\nbot = socialagent.SocialAgentApp(config)\n"
        "async def hook(msg):\n    await bot.state.get('p', 'k')\n",
        "async def h(data, ctx):\n    store = ctx.state\n    x = 1\n"
        "    await store.get('p', 'k')\n",
        "class Flow:\n    async def run(self):\n        x = 1\n"
        "        await self.ctx.state.get('p', 'k')\n",
        "from socialagent import SocialAgentApp\napp: SocialAgentApp = SocialAgentApp(c)\n"
        "async def hook(msg):\n    await app.state.get('p', 'k')\n",
        "async def h(data, ctx):\n    store: object = ctx.state\n    x = 1\n"
        "    await store.get('p', 'k')\n",
        "async def h(data, ctx):\n    if (store := ctx.state):\n        x = 1\n"
        "        await store.get('p', 'k')\n",
    ],
)
def test_lambda_side_state_counts_through_the_app_an_alias_or_an_attribute(
    source: str,
) -> None:
    assert state_call_lines(ast.parse(source)) == [4]


@pytest.mark.parametrize(
    "source",
    [
        "async def h(data, ctx):\n"
        "    await ctx.state.get('p', 'k')\n"
        "    await self.state.get('p', 'k')\n",
        "async def h(data, c: EventContext):\n"
        "    await c.state.get('p', 'k')\n"
        "    await self.state.get('p', 'k')\n",
    ],
)
def test_the_handler_context_is_found_by_name_or_annotation(source: str) -> None:
    # Line 2 is the handler context's state; line 3, an object's own, is not.
    assert state_call_lines(ast.parse(source)) == [2]


def test_ctx_counts_only_in_a_handler_file() -> None:
    """Outside the handlers, ``ctx`` is some other context (``click.Context``);
    an ``EventContext`` annotation still counts anywhere."""
    click = ast.parse("def cmd(ctx):\n    ctx.state.get('k')\n")
    assert state_call_lines(click, handler=False) == []
    assert state_call_lines(click, handler=True) == [2]
    for annotation in (
        "EventContext",
        "Optional[EventContext]",
        "EventContext | None",
        "'EventContext'",
    ):
        annotated = ast.parse(f"def h(c: {annotation}):\n    c.state.get('p', 'k')\n")
        assert state_call_lines(annotated, handler=False) == [2], annotation


def test_a_blueprint_file_that_does_not_parse_fails_the_check(
    tmp_path: Path, monkeypatch
) -> None:
    """The deploy skips a file it cannot parse; the blueprint check must not,
    or a broken blueprint file would hide its state calls."""
    (tmp_path / "socialagent.dev.yaml").write_text("project: p\n")
    (tmp_path / "agent.py").write_text("def (:\n")
    monkeypatch.setattr(sys.modules[__name__], "get_blueprint_dir", lambda _: tmp_path)
    with pytest.raises(SyntaxError):
        _state_calls("broken")
    assert state_calls({}, tmp_path) == []


@pytest.mark.parametrize("blueprint", _BLUEPRINT_NAMES)
def test_scaffolded_tfvars_carry_the_flag(
    blueprint: str, tmp_path: Path, monkeypatch
) -> None:
    """The flag reaches Terraform from a fresh scaffold: `socialagent init`
    writes the YAML, `load()` validates it, and the tfvars `socialagent deploy`
    renders from it carry the flag. test_cli_tfvars.py covers the mapping on a
    hand-built config; this covers what a new project actually ships."""
    monkeypatch.chdir(tmp_path)
    cmd_init(
        argparse.Namespace(name="my-bot", blueprint=blueprint, list_blueprints=False)
    )
    project = tmp_path / "my-bot"
    config = load(project / "socialagent.dev.yaml")
    out = generate(config, project, REPO_ROOT / "infra-terraform")
    wanted = bool((_config(blueprint).get("features") or {}).get("state_table"))
    assert out.get("enable_state_table", False) is wanted, (
        f"{blueprint}: features.state_table={wanted} did not reach the tfvars"
    )
