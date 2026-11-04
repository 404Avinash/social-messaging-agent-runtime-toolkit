# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The appointment-scheduler prompt and catalog agree with WhatsApp's limits.

rich-messages refuses a button label over 20 characters and a row title over
24, so a prompt that asks for "name — price" in a label costs a failed tool
call and a retry. The prices belong in the body, and every listing name must
fit a button on its own. ``agent.py`` starts the server on import, so its
prompt is read from the source with ``ast``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

BLUEPRINT = Path(__file__).resolve().parents[1] / "blueprints" / "appointment-scheduler"


def _system_prompt() -> str:
    tree = ast.parse((BLUEPRINT / "agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_SYSTEM_PROMPT_TEMPLATE"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError("_SYSTEM_PROMPT_TEMPLATE not found in agent.py")


def test_prompt_puts_prices_in_the_body_not_in_labels():
    prompt = _system_prompt()
    assert "prices go in the body" in prompt
    assert "listing name alone" in prompt


def test_every_listing_name_fits_a_button_label():
    spec = importlib.util.spec_from_file_location(
        "_scheduler_catalog", BLUEPRINT / "src" / "catalog.py"
    )
    catalog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(catalog)  # type: ignore[union-attr]
    too_long = [p["name"] for p in catalog.LISTINGS if len(p["name"]) > 20]
    assert not too_long, f"listing names over WhatsApp's 20-character label: {too_long}"


def test_the_slot_confirmation_is_always_a_buttons_message():
    """Half the live turns confirmed in plain text, so the user could not tap Yes.

    The skill's confirmation step said "as text" while another rule required
    Yes/No buttons; the model picked one at random. The step now names the
    buttons call, and nothing in the skill or the prompt may ask for a text
    confirmation again.
    """
    skill = (BLUEPRINT / "src" / "skills" / "slot_selection.md").read_text()
    confirm_step = next(
        line
        for line in skill.splitlines()
        if line.startswith("3. Once a slot is selected")
    )
    assert (
        'message_type="buttons"' in confirm_step and 'options="Yes,No"' in confirm_step
    )
    assert "as text" not in confirm_step
    assert "`Sí,No`" in confirm_step, "the labels follow the conversation language"
    cancellation = (BLUEPRINT / "src" / "skills" / "cancellation.md").read_text()
    # The cancellation confirmation is owned by execute_action("cancel_visit") itself
    # (tests/test_appointment_scheduler_cancel_confirmation.py drives it).
    assert "confirmation_requested" in cancellation
    for text, name in (
        (skill, "slot skill"),
        (cancellation, "cancellation skill"),
        (_system_prompt(), "prompt"),
    ):
        assert 'ask "Confirm' not in text, (
            f"the {name} still asks for a plain-text confirmation"
        )


def _prompt_constant(agent_py: Path) -> str:
    """The module-level prompt string of a blueprint agent.py, read with ast (importing
    would start the server)."""
    tree = ast.parse(agent_py.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.endswith(("PROMPT", "PROMPT_TEMPLATE"))
            for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise AssertionError(f"no module-level *PROMPT constant in {agent_py}")


def test_llm_blueprints_tell_the_model_that_restored_history_hides_tool_calls():
    """Restored sessions keep each turn's text and drop its tool blocks
    (packages/socialagent-runtime/_history.py). A model that sees "done, saved"
    with no call behind it, and then a tool reporting the current state, concluded
    in two live rounds that its earlier turns were lies and apologized for them.
    Every blueprint that runs a model states the rule, in the prompt itself."""
    root = BLUEPRINT.parent
    for name in ("personal-assistant", "appointment-scheduler"):
        prompt = _prompt_constant(root / name / "agent.py")
        assert "restored as text only" in prompt and "tool calls behind" in prompt, (
            f"{name}: the system prompt does not explain that restored history hides tool calls"
        )


def test_the_scheduler_prompt_points_at_its_state_source_not_at_a_tool():
    """The blueprint has no tool that reads state; Current User Status in the
    prompt is its source of truth, and status_check.md forbids tool calls."""
    prompt = _prompt_constant(BLUEPRINT / "agent.py")
    assert "check the Current User Status above" in prompt
    assert "check the current state with a tool" not in prompt
