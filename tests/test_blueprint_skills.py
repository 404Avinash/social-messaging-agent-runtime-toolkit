# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The blueprints' skill loaders only read skill files.

The agent chooses the name passed to ``load_skill``, so it is untrusted: a
loader that joins it into a path must keep the result inside ``src/skills/``.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(blueprint: str, monkeypatch: pytest.MonkeyPatch):
    path = REPO_ROOT / "blueprints" / blueprint / "src" / "skills.py"
    name = f"_skills_{blueprint.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, name, module)
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def appointment_skills(monkeypatch):
    return _load("appointment-scheduler", monkeypatch)


def test_appointment_loader_reads_a_shipped_skill(appointment_skills):
    expected = (appointment_skills._SKILLS_DIR / "cancellation.md").read_text()
    assert appointment_skills.load_skill("cancellation") == expected


@pytest.mark.parametrize(
    "name",
    [
        "../../AGENTS",  # blueprints/appointment-scheduler/AGENTS.md
        "../../SOCIALAGENT_GUIDE",
        "../skills/cancellation",  # resolves inside, but carries a separator
        "..",
        ".",
        "cancellation.md",
        ".hidden",
        "sub/skill",
        "sub\\skill",
        "",
        "a" * 65,
        None,
    ],
)
def test_appointment_loader_refuses_names_that_are_not_a_bare_stem(
    appointment_skills, name
):
    out = appointment_skills.load_skill(name)
    assert out.startswith(f"Skill '{name}' not found.")


def test_appointment_loader_refuses_an_absolute_path(appointment_skills, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("outside the skills directory")
    out = appointment_skills.load_skill(str(tmp_path / "secret"))
    assert "outside the skills directory" not in out
    assert "not found" in out


def test_appointment_loader_refuses_a_symlink_out_of_the_directory(
    appointment_skills, tmp_path, monkeypatch
):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "real.md").write_text("a real skill")
    (tmp_path / "outside.md").write_text("outside the skills directory")
    (skills / "escape.md").symlink_to(tmp_path / "outside.md")
    monkeypatch.setattr(appointment_skills, "_SKILLS_DIR", skills)
    assert appointment_skills.load_skill("real") == "a real skill"
    out = appointment_skills.load_skill("escape")
    assert "outside the skills directory" not in out
    assert "not found" in out


def test_personal_assistant_loader_never_builds_a_path_from_the_name(monkeypatch):
    """This loader looks names up among the files it globbed, so a traversal
    name is simply not found. Pinned so a rewrite cannot regress it."""
    skills = _load("personal-assistant", monkeypatch)
    assert skills.load_skill("scheduling")
    for name in ("../../AGENTS", "../skills/scheduling", "/etc/hosts"):
        assert skills.load_skill(name).startswith(f"Skill '{name}' not found.")


# ── personal-assistant scheduling skill ─────────────────────────────────────────

_PA = REPO_ROOT / "blueprints" / "personal-assistant"


def _personal_assistant_tools() -> set[str]:
    """The tool names in ``socialagent_tools``, read without importing strands."""
    tree = ast.parse((_PA / "src" / "tools" / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "socialagent_tools"
            for t in node.targets
        ):
            return {elt.id for elt in node.value.elts}
    raise AssertionError("socialagent_tools not found")


def test_personal_assistant_lists_its_recurring_operations(monkeypatch):
    """The prompt names the operations schedule_recurring accepts, so the
    model does not invent one that no handler runs."""
    skills = _load("personal-assistant", monkeypatch)
    index = skills.load_skill_index(accepted={"reminder", "invoke_agent"})
    assert "## Registered Scheduler Operations" in index
    assert "- `reminder`:" in index
    assert "- `invoke_agent`:" in index


def test_personal_assistant_skills_name_only_tools_it_has():
    """A skill that describes a tool the bot lacks invites the model to fake
    its result (the scheduling skill once offered weather and search)."""
    tools = _personal_assistant_tools()
    for path in sorted((_PA / "src" / "skills").glob("*.md")):
        named = set(re.findall(r"`([a-z_]+)\(", path.read_text()))
        named |= set(re.findall(r"^#+ ([a-z_]+)\(", path.read_text(), re.M))
        assert named <= tools, f"{path.name} names {sorted(named - tools)}"


def test_personal_assistant_scheduling_crons_are_accepted():
    """Every cron the skill shows passes the runtime's own validator and has
    the year field of ``*`` that schedule_recurring takes, and the skill says
    that a year is refused."""
    sys.path.insert(0, str(REPO_ROOT / "packages" / "socialagent-runtime"))
    try:
        from scheduler import _validate_cron_inner
    finally:
        sys.path.pop(0)

    text = (_PA / "src" / "skills" / "scheduling.md").read_text()
    crons = re.findall(r'cron="([^"]+)"', text)
    field = r"[0-9A-Z*?,/#-]+"
    crons += re.findall(rf"`((?:{field} ){{5}}{field})`", text)
    assert len(crons) >= 5
    for cron in crons:
        assert _validate_cron_inner(cron) == cron
        assert cron.split()[5] == "*", cron
    section = _scheduling_section("schedule_recurring")
    assert re.search(r"year.*`\*`", section), "the skill does not say the year is *"


def _tools_that_send_messages() -> set[str]:
    """The tools that deliver through ``notify.send_message``. Inside a turn
    that queues the message for the webhook, and a scheduled task's handler
    sends only the text result, so from a scheduled task they deliver
    nothing."""
    tree = ast.parse((_PA / "src" / "tools" / "__init__.py").read_text())
    senders = set()
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "send_message"
            for call in ast.walk(node)
        ):
            senders.add(node.name)
    return senders


def _scheduling_section(heading: str) -> str:
    text = (_PA / "src" / "skills" / "scheduling.md").read_text()
    match = re.search(rf"^### {re.escape(heading)}\(.*?(?=^#)", text, re.M | re.S)
    assert match, heading
    return match.group(0)


def test_personal_assistant_schedules_no_task_that_answers_in_media():
    """A scheduled task's voice note, image or interactive message never
    arrives, so the skill must not offer one and must say why."""
    senders = _tools_that_send_messages()
    assert senders == {"voice_reply", "generate_image_tool", "attach_interactive"}
    section = _scheduling_section("schedule_task")
    examples = re.findall(r'^- ".*', section, re.M)
    assert len(examples) >= 3
    for example in examples:
        assert not re.search(r"voice|audio|image|picture|photo|button", example, re.I)
    for name in senders:
        assert f"`{name}`" in section, f"schedule_task does not warn about {name}"


def test_personal_assistant_skill_says_what_a_task_cannot_schedule():
    """schedule_task refuses a task due sooner than the cron floor, and the
    tools that create schedules refuse inside a task, so the skill says both,
    with the floor the runtime enforces, or the agent offers what fails."""
    sys.path.insert(0, str(REPO_ROOT / "packages" / "socialagent-runtime"))
    try:
        from scheduler import MIN_CRON_INTERVAL_MINUTES
    finally:
        sys.path.pop(0)

    section = _scheduling_section("schedule_task")
    assert f"at least {MIN_CRON_INTERVAL_MINUTES} minutes" in section
    assert "cannot create schedules" in section
    for name in ("schedule_reminder", "schedule_task", "schedule_recurring"):
        assert f"`{name}`" in section, (
            f"schedule_task does not say {name} refuses in a task"
        )


def test_personal_assistant_moves_a_schedule_by_replacing_it():
    """A new time is a new schedule, not an update of the old one.

    Auto-generated ids hash the timing, so "change my 9am water reminder to
    10" re-issued as a new reminder left both firing every day. The skill
    and every tool that creates a schedule say to cancel the old one first.
    """
    text = (_PA / "src" / "skills" / "scheduling.md").read_text()
    rules = re.search(r"^## Rules\n(.*?)(?=^## )", text, re.M | re.S).group(1)
    moves = [
        line for line in rules.splitlines() if re.search(r"\bchange\b", line, re.I)
    ]
    assert moves, "no rule says how to change a schedule"
    assert "list_schedules" in moves[0] and "cancel_schedule" in moves[0]

    tree = ast.parse((_PA / "src" / "tools" / "__init__.py").read_text())
    docs = {
        node.name: ast.get_docstring(node) or ""
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    for name in ("schedule_reminder", "schedule_task", "schedule_recurring"):
        assert "cancel_schedule" in docs[name], name
