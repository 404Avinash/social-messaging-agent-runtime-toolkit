# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the rich-messages recipe's payload construction.

Recipes are copied verbatim into user projects and run agent-side, so their
tool.py logic is worth testing even though it lives outside the package. Loaded by
path, since `recipes/rich-messages/` is not an importable module.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from socialagent.providers.utils import interactive_reply_prompt
from socialagent.types import MessageType

_RECIPE = Path(__file__).parent.parent / "recipes" / "rich-messages" / "tool.py"


@pytest.fixture(scope="module")
def rich():
    # conftest puts socialagent_runtime on the path and stubs bedrock_agentcore, which the
    # recipe imports transitively.
    spec = importlib.util.spec_from_file_location("_rich_messages_recipe", _RECIPE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_rows_dedups_colliding_ids(rich):
    """Two rows with the same title must not produce the same WhatsApp row id.

    WhatsApp rejects the whole interactive message on a duplicate row id, so the
    user would get plain text and no menu.
    """
    rows = rich._parse_rows("Consulta~9:00;Consulta~11:00")

    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), f"duplicate row ids: {ids}"
    assert [r["title"] for r in rows] == ["Consulta", "Consulta"]


def test_parse_rows_rejects_a_title_over_24_chars(rich):
    """A title WhatsApp would cut is refused, not cut; the agent gets the row back."""
    with pytest.raises(ValueError, match="24"):
        rich._parse_rows("Servicio de consulta general A~x;Corto~y")


def test_location_accepts_zero_coordinates(rich):
    """Lat/lon of exactly 0 (equator / prime meridian) is valid, not missing."""
    result = rich.build_rich_message(
        message_type="location", latitude=0.0, longitude=0.0, name="Null Island"
    )
    assert "error" not in result


def test_location_missing_coordinates_still_rejected(rich):
    result = rich.build_rich_message(message_type="location", name="nowhere")
    assert result.get("error")


# --- WhatsApp length limits are errors, not silent truncation ----------------
#
# A row title is capped at 24 characters and a button label at 20. Cutting them
# garbles the message ("Parkview Apartment — $540,000" reached the phone as
# "Parkview Apartment — $54") with no signal to anyone. The recipe returns an
# error instead, the same channel it uses for a malformed options string, so
# the agent shortens the text and retries.


def test_list_row_title_over_24_chars_is_an_error_naming_the_row(rich):
    result = rich.build_rich_message(
        "list_menu",
        body="Which one?",
        options="Homes|Parkview Apartment — $540,000~Nice;Garden House~Also nice",
    )
    assert "error" in result
    assert "24" in result["error"] and "Parkview Apartment" in result["error"]


def test_list_row_title_at_24_chars_is_fine(rich):
    title = "x" * 24
    result = rich.build_rich_message(
        "list_menu", body="Which?", options=f"S|{title}~d;y~e"
    )
    assert "error" not in result


def test_button_label_over_20_chars_is_an_error_naming_the_label(rich):
    result = rich.build_rich_message(
        "buttons", body="Pick", options="Downtown Loft,Parkview Apartment Deluxe"
    )
    assert "error" in result
    assert "20" in result["error"] and "Parkview Apartment Deluxe" in result["error"]


# Both blueprints ship a pre-wired copy of the recipe, so the list and button
# checks below run against all three: a fix made to one copy cannot be missed
# in another.
_BLUEPRINTS = Path(__file__).parent.parent / "blueprints"
_COPIES = {
    "recipe": _RECIPE,
    "personal-assistant": _BLUEPRINTS / "personal-assistant/src/tools/rich_messages.py",
    "appointment-scheduler": _BLUEPRINTS
    / "appointment-scheduler/src/tools/rich_messages.py",
}


@pytest.fixture(scope="module", params=sorted(_COPIES))
def builder(request):
    name = "_rich_messages_" + request.param.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, _COPIES[request.param])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row_ids(message: dict) -> list[str]:
    sections = message["payload"]["interactive"]["action"]["sections"]
    return [row["id"] for section in sections for row in section["rows"]]


def test_list_row_ids_unique_within_a_section(builder):
    result = builder.build_rich_message(
        message_type="list_menu",
        body="Pick a time",
        options="Haircut~9:00;Haircut~11:00",
    )

    assert "error" not in result, result
    ids = _row_ids(result["message"])
    assert len(ids) == len(set(ids)), f"duplicate row ids: {ids}"


def test_list_row_ids_unique_across_sections(builder):
    """The same time under two days is the usual way to collide, and WhatsApp
    checks row ids across the whole message, not per section."""
    result = builder.build_rich_message(
        message_type="list_menu",
        body="Pick a slot",
        options="Monday|10:00 AM~Haircut on Monday;11:00 AM~Haircut on Monday"
        "|Tuesday|10:00 AM~Haircut on Tuesday;11:00 AM~Haircut on Tuesday",
    )

    assert "error" not in result, result
    ids = _row_ids(result["message"])
    assert len(ids) == 4
    assert len(ids) == len(set(ids)), f"duplicate row ids: {ids}"


def test_list_row_ids_stay_derived_from_the_title(builder):
    """Only a colliding row gets a suffix; the first keeps its readable id."""
    result = builder.build_rich_message(
        message_type="list_menu",
        body="Pick a slot",
        options="Lunes|10:00~Corte el lunes|Martes|10:00~Corte el martes",
    )

    assert "error" not in result, result
    assert _row_ids(result["message"]) == ["10:00", "10:00_2"]


@pytest.mark.parametrize(
    "options",
    [
        # The same slot under two days: the section title is not in the reply.
        "Monday|10:00 AM~Haircut;11:00 AM~Haircut|Tuesday|10:00 AM~Haircut",
        # Repeated within one section.
        "Haircut~9:00;Haircut~9:00",
        # Different only in letter case.
        "Monday|10:00 AM~Haircut|Tuesday|10:00 am~haircut",
        # Split differently, but the reply joins title and description with " — ".
        "10:00 — Corte~Lunes;10:00~Corte — Lunes",
    ],
)
def test_list_rows_that_read_the_same_are_rejected(builder, options):
    """A tap reaches the agent as the row's title and description only, so two
    rows that read the same are two choices it cannot tell apart."""
    result = builder.build_rich_message(
        message_type="list_menu", body="Pick a slot", options=options
    )

    assert "message" not in result
    assert "differ" in result["error"]


@pytest.mark.parametrize(
    "options",
    [
        "Monday|10:00 AM~Haircut on Monday;11:00 AM~Haircut on Monday"
        "|Tuesday|10:00 AM~Haircut on Tuesday;11:00 AM~Haircut on Tuesday",
        "Lunes|10:00~Corte el lunes|Martes|10:00~Corte el martes",
        "Morning|Coffee~Before noon;Tea~Before noon|Evening|Coffee~After six",
    ],
)
def test_list_replies_tell_same_titled_rows_apart(builder, options):
    """A list that is sent must give the agent a different reply for every row:
    the webhook builds the prompt from the tapped row's title and description,
    and nothing in it names the section."""
    result = builder.build_rich_message(
        message_type="list_menu", body="Pick a slot", options=options
    )

    assert "error" not in result, result
    sections = result["message"]["payload"]["interactive"]["action"]["sections"]
    prompts = [
        interactive_reply_prompt(
            {"interactive": {"type": "list_reply", "list_reply": row}},
            MessageType.LIST_REPLY,
        )
        for section in sections
        for row in section["rows"]
    ]
    assert len(prompts) == len(set(prompts)), prompts


def test_list_repeat_trimmed_by_the_row_limit_is_not_rejected(builder):
    """Only rows that are delivered can be confused, and the 10-row limit drops
    an 11th row before it is sent."""
    rows = ";".join(f"Slot {n}~Haircut" for n in range(1, 11))
    result = builder.build_rich_message(
        message_type="list_menu", body="Pick a slot", options=f"{rows};Slot 1~Haircut"
    )

    assert "error" not in result, result
    assert len(_row_ids(result["message"])) == 10


def test_buttons_with_repeated_labels_are_rejected(builder):
    result = builder.build_rich_message(
        message_type="buttons", body="Sure?", options="Yes,No,Yes"
    )

    assert "message" not in result
    assert result.get("error")


def test_buttons_with_distinct_labels_still_build(builder):
    result = builder.build_rich_message(
        message_type="buttons", body="Sure?", options="Yes,No"
    )

    assert "error" not in result, result
