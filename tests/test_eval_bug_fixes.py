# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for framework and blueprint behaviours.

Covers:
- filter_restored_tool_context stays off; _history.py does the stripping (framework)
- language/format-robust slot resolution in the appointment-scheduler catalog
- empty SCHEDULER_LAMBDA_ARN falls back to the notification Lambda (framework)
- Guard: unparseable date+time must not silently match the first slot
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent

# socialagent-runtime files are importable by filename (vendored package pattern)
_AGENT_PATH = str(_REPO / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import config as agent_config_mod  # noqa: E402


def _load_catalog():
    spec = importlib.util.spec_from_file_location(
        "appt_catalog",
        _REPO / "blueprints" / "appointment-scheduler" / "src" / "catalog.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Don't drop __pycache__ into the blueprint tree — blueprints are copied
    # file-by-file by `socialagent init` and must stay free of interpreter artifacts.
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


catalog = _load_catalog()


# --- SCHEDULER_LAMBDA_ARN="" must fall back to NOTIFICATION_LAMBDA_ARN ---


def test_empty_scheduler_arn_falls_back_to_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "arn:aws:lambda:::function:notify")
    # Terraform sets the var to "" (present but empty) when enable_scheduler_lambda=false
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "")
    cfg = agent_config_mod.AgentConfig.from_env()
    assert cfg.scheduler_lambda_arn == "arn:aws:lambda:::function:notify"


def test_absent_scheduler_arn_falls_back_to_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "arn:aws:lambda:::function:notify")
    monkeypatch.delenv("SCHEDULER_LAMBDA_ARN", raising=False)
    cfg = agent_config_mod.AgentConfig.from_env()
    assert cfg.scheduler_lambda_arn == "arn:aws:lambda:::function:notify"


def test_explicit_scheduler_arn_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "arn:aws:lambda:::function:notify")
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "arn:aws:lambda:::function:sched")
    cfg = agent_config_mod.AgentConfig.from_env()
    assert cfg.scheduler_lambda_arn == "arn:aws:lambda:::function:sched"


# --- session restore must strip historical tool context ---


def test_session_manager_owns_restored_tool_context() -> None:
    """create_session_manager keeps the SDK's filter_restored_tool_context OFF.

    A restored window must not begin on a toolResult whose toolUse fell outside
    the window (Bedrock rejects the request), so tool blocks are stripped on
    restore. The SDK's filter drops the message it empties, which erases every
    assistant turn of a bot that answers through tools; the runtime's own
    filter (_history.py) strips the blocks and keeps the turn as the text the
    user saw, so the SDK flag must stay off or it runs first and wins.
    """
    import socialagent_runtime  # conftest wires the vendored package + mocks bedrock_agentcore

    from unittest.mock import patch as _patch

    class _FakeAgentCoreMemorySessionManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_messages(self, *args, **kwargs):
            return []

    from bedrock_agentcore.memory.integrations.strands import (
        session_manager as session_manager_mod,
    )

    payload = {
        "prompt": "hola",
        "socialagent": {
            "version": 1,
            "memory_id": "mem-123",
            "session_id": "sess-1",
            "actor_id": "actor-1",
        },
    }
    with (
        _patch.object(socialagent_runtime, "_install_session_read_guard"),
        _patch.object(
            session_manager_mod,
            "AgentCoreMemorySessionManager",
            _FakeAgentCoreMemorySessionManager,
        ),
    ):
        manager = socialagent_runtime.create_session_manager(payload)

    assert isinstance(manager, _FakeAgentCoreMemorySessionManager)
    assert type(manager).__name__ == "ReplaySafeAgentCoreMemorySessionManager"

    # bedrock_agentcore is a MagicMock (see conftest) — inspect the config call
    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig,
    )

    _, kwargs = AgentCoreMemoryConfig.call_args
    assert kwargs.get("filter_restored_tool_context") is False


# --- slot resolution by numeric day + hour (+ guard) ---


def _first_slot():
    property_id = catalog.LISTINGS[0]["id"]
    return property_id, catalog.SLOTS.get(property_id)[0]


def test_resolve_slot_exact_english() -> None:
    property_id, slot = _first_slot()
    assert (
        catalog.resolve_slot_by_datetime(property_id, slot["date"], slot["time"])
        == slot
    )


def test_resolve_slot_spanish_date_and_24h_time() -> None:
    """'viernes 17 de julio' + '16:00' must match 'Friday, July 17' + '4:00 PM'."""
    property_id, slot = _first_slot()
    day = next(t for t in slot["date"].split() if t.isdigit())
    hour12 = int(slot["time"].split(":")[0])
    hour24 = hour12 if ("AM" in slot["time"] or hour12 == 12) else hour12 + 12
    spanish_date = f"viernes {day} de julio"
    time_24h = f"{hour24}:00"
    assert catalog.resolve_slot_by_datetime(property_id, spanish_date, time_24h) == slot


def test_resolve_slot_bare_hour_with_meridiem() -> None:
    property_id, slot = _first_slot()
    bare = slot["time"].replace(":00", "")  # "9:00 AM" -> "9 AM"
    assert catalog.resolve_slot_by_datetime(property_id, slot["date"], bare) == slot


def test_resolve_slot_unparseable_returns_none() -> None:
    """Neither field parseable → refuse, never silently return the first slot."""
    property_id, _ = _first_slot()
    assert (
        catalog.resolve_slot_by_datetime(property_id, "mañana", "por la tarde") is None
    )


def test_resolve_slot_one_sided_parse_returns_none() -> None:
    """A day without an hour (or vice versa) is ambiguous — a day has several
    slots and an hour repeats across days — so it must refuse, not pick one."""
    property_id, slot = _first_slot()
    day = next(t for t in slot["date"].split() if t.isdigit())
    assert (
        catalog.resolve_slot_by_datetime(property_id, f"day {day}", "por la tarde")
        is None
    )
    assert catalog.resolve_slot_by_datetime(property_id, "mañana", slot["time"]) is None


def test_resolve_slot_wrong_day_returns_none() -> None:
    property_id, slot = _first_slot()
    # Day 32 never exists in the catalog
    assert catalog.resolve_slot_by_datetime(property_id, "day 32", slot["time"]) is None


def test_hour_24_parsing() -> None:
    assert catalog._hour_24("4:00 PM") == 16
    assert catalog._hour_24("16:00") == 16
    assert catalog._hour_24("12:00 PM") == 12
    assert catalog._hour_24("12:00 AM") == 0
    assert catalog._hour_24("sin hora") is None
