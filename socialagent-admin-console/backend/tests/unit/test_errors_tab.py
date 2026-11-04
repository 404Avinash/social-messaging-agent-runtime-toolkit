# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Errors tab shows the newest matching log lines, not the first page.

filter_log_events returns events oldest first. With a single call and a small
limit, a busy day's first errors filled the tab and the failure that just
happened (for example a send-message the console reported as failed) never
appeared. The tab now reads back from now: the last 15 minutes, then the rest of
the hour, of 6 hours and of the day, halving any range too busy to read whole.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.unit.fake_logs import FakeLogs, newest, spread

HOUR_MS = 3_600_000
GROUP = "/aws/lambda/dev-bot-notification"


def _events(prefix, count, base):
    return [{"timestamp": base + i, "message": f"[ERROR] {prefix} {i}"} for i in range(count)]


def _run(side_effect, **kw):
    """The search's LogRead on one group, and the client it read with."""
    from routes import stacks

    logs = MagicMock()
    logs.filter_log_events.side_effect = side_effect
    original = stacks.logs
    stacks.logs = logs
    try:
        return stacks._newest_log_events("/aws/lambda/dev-bot-notification", "?ERROR", hours=24, **kw), logs
    finally:
        stacks.logs = original


def _window_hours(call):
    return round((call.kwargs["endTime"] - call.kwargs["startTime"]) / HOUR_MS, 2)


def test_a_busy_last_quarter_hour_is_enough():
    read, logs = _run(lambda **kw: {"events": _events("recent", 12, kw["startTime"])})

    assert [e["message"] for e in read.events] == [f"[ERROR] recent {i}" for i in range(2, 12)]
    assert read.reason is None
    assert [_window_hours(c) for c in logs.filter_log_events.call_args_list] == [0.25]


def _errors(label: str, count: int, newest_minutes_ago: float, oldest_minutes_ago: float) -> list[dict]:
    return [
        {"timestamp": ts, "message": f"[ERROR] {label} {i}"}
        for i, ts in enumerate(spread(count, newest_minutes_ago, oldest_minutes_ago))
    ]


def _read(fake: FakeLogs, **kw):
    """The search's LogRead on GROUP, read with ``fake``."""
    from routes import stacks

    original = stacks.logs
    stacks.logs = fake
    try:
        return stacks._newest_log_events(GROUP, "ERROR", hours=24, **kw)
    finally:
        stacks.logs = original


def test_quiet_recent_windows_widen_until_ten_are_found():
    lines = (
        _errors("w6", 30, 70, 350)
        + _errors("w1", 2, 20, 50)
        + _errors("w0.25", 1, 5, 5)
        + _errors("old", 30, 400, 1400)
    )
    fake = FakeLogs({GROUP: lines})

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10)
    calls = fake.filter_log_events.call_args_list
    assert [_window_hours(c) for c in calls] == [0.25, 0.75, 5]
    # Each range is read once: the next one ends where the last one started.
    assert all(b.kwargs["endTime"] == a.kwargs["startTime"] - 1 for a, b in zip(calls, calls[1:]))


def test_fewer_than_ten_in_a_day_returns_them_all():
    lines = _errors("only", 3, 30, 1000)
    fake = FakeLogs({GROUP: lines})

    read = _read(fake)

    assert [e["message"] for e in read.events] == ["[ERROR] only 0", "[ERROR] only 1", "[ERROR] only 2"]
    # The whole day was read: fewer than ten is the answer, not a short read.
    assert read.reason is None
    assert [_window_hours(c) for c in fake.filter_log_events.call_args_list] == [0.25, 0.75, 5, 18]


def test_a_range_too_busy_to_read_whole_is_halved_newest_half_first():
    """600 errors in the quarter hour: its first page holds the oldest 100."""
    lines = _errors("busy", 600, 0.1, 14)
    fake = FakeLogs({GROUP: lines})

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10)
    first, second = (c.kwargs for c in fake.filter_log_events.call_args_list[:2])
    assert second["endTime"] == first["endTime"]
    assert abs(second["startTime"] - (first["startTime"] + first["endTime"]) / 2) <= 1


def test_a_heavy_quarter_hour_still_gives_its_newest_lines():
    """20 errors a second for 15 minutes: halving each range after its first
    page reaches the last seconds within the call budget.

    Reading a range for the full page bound before halving it spent five
    calls per halving, so the budget ran out on a two-minute range whose
    pages held its oldest lines, a minute or more old.
    """
    from routes import stacks

    lines = _errors("busy", 18_000, 0.01, 14.9)
    fake = FakeLogs({GROUP: lines})

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10)
    calls = fake.filter_log_events.call_args_list
    assert len(calls) <= stacks._MAX_CALLS_PER_SEARCH
    # A range that can still be halved is never read past its first page.
    for call in calls:
        if "nextToken" in call.kwargs:
            assert call.kwargs["endTime"] - call.kwargs["startTime"] < 2 * stacks._MIN_SPLIT_MS


def test_quiet_minutes_after_a_busy_hour_keep_the_newest_of_both():
    """The last 10 minutes, then the newest of an hour too busy to read whole."""
    lines = _errors("busy", 600, 70, 350) + _errors("recent", 3, 2, 10)
    fake = FakeLogs({GROUP: lines})

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10)
    assert events[-1]["message"] == "[ERROR] recent 2"


def test_a_log_group_whose_pages_never_end_stops_at_the_call_budget():
    from routes import stacks

    # Every page has more, and the newest ranges hold fewer lines than asked
    # for, so the search reads on until the budget is spent.
    read, logs = _run(
        lambda **kw: {"events": _events("busy", 2, kw["startTime"]), "nextToken": "more"}, keep=50
    )

    assert logs.filter_log_events.call_count == stacks._MAX_CALLS_PER_SEARCH
    assert read.events and all("busy" in e["message"] for e in read.events)
    assert read.reason == stacks.UNREAD_BUDGET


def test_a_failed_page_keeps_what_was_read():
    calls = []

    def side_effect(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError("throttled")
        return {"events": _events("first", 4, kw["startTime"]), "nextToken": "next"}

    read, _ = _run(side_effect)

    assert [e["message"] for e in read.events] == [f"[ERROR] first {i}" for i in range(4)]
    # Not a botocore error, so a fault: the reason is its type.
    assert read.reason == "RuntimeError"


def test_errors_tab_lists_the_newest_lines_per_lambda():
    from routes import stacks

    logs = MagicMock()
    logs.filter_log_events.side_effect = lambda **kw: {"events": _events("new", 12, kw["startTime"])}
    original_logs, original_registry = stacks.logs, stacks._get_registry
    stacks.logs = logs
    stacks._get_registry = lambda _id: {"outputs": {"webhook_lambda": "dev-bot-webhook"}}
    try:
        resp = stacks.handle_get_errors({"id": "bot-dev"}, {})
    finally:
        stacks.logs, stacks._get_registry = original_logs, original_registry

    body = json.loads(resp["body"])
    messages = [e["message"] for e in body["results"]]
    assert "[ERROR] new 11" in messages and "[ERROR] new 0" not in messages
    assert body["unreadable"] == []


def _rare_errors_in_a_busy_group() -> list[dict]:
    """2000 info lines in the last quarter hour, and 5 errors among them 10-14 minutes ago."""
    info = [{"timestamp": ts, "message": f"[INFO] line {i}"} for i, ts in enumerate(spread(2000, 0.01, 14.9))]
    return info + _errors("rare", 5, 10, 14)


def _is_error(event: dict) -> bool:
    return "ERROR" in event["message"]


def test_rare_errors_in_a_busy_group_are_all_found():
    """The API returns partial, even empty, pages with a nextToken while lines
    are left to scan. Taking any page with a token as a range too busy to read
    whole halved the quarter hour into its last seconds, which held none of
    the five errors, and the tab showed nothing."""
    from routes import stacks

    lines = _rare_errors_in_a_busy_group()
    fake = FakeLogs({GROUP: lines}, scan=200)

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10, _is_error)
    assert len(events) == 5
    calls = fake.filter_log_events.call_args_list
    assert len(calls) <= stacks._MAX_CALLS_PER_SEARCH
    assert not any("startFromHead" in c.kwargs for c in calls)


def test_a_botocore_that_reads_newest_first_is_asked_to():
    lines = _rare_errors_in_a_busy_group() + _errors("recent", 12, 0.5, 3)
    fake = FakeLogs({GROUP: lines}, scan=200, newest_first=True)

    events = _read(fake).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10, _is_error)
    calls = fake.filter_log_events.call_args_list
    assert all(c.kwargs["startFromHead"] is False for c in calls)
    # One range over the whole day, read back from now until ten are found.
    assert {_window_hours(c) for c in calls} == {24}
    assert len(calls) <= 3


# botocore added startFromHead to FilterLogEvents in 1.43.33.
_NEWEST_FIRST_BOTOCORE = (1, 43, 33)


def test_the_package_ships_a_botocore_that_reads_newest_first():
    """The backend ran on the python3.12 runtime's bundled SDK, which predates
    startFromHead, so production always took the window-by-window fallback,
    which follows a busy group's pages from the oldest end and can spend its
    budget before it reaches the last minutes. requirements.txt pins an SDK
    that has it, and the Terraform packaging step installs it into the zip."""
    backend = Path(__file__).resolve().parents[2]
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (backend / name).read_text()
        floors = dict(re.findall(r"^(boto3|botocore)>=([0-9.]+)\s*$", text, re.M))
        assert set(floors) == {"boto3", "botocore"}, name
        for package, floor in floors.items():
            assert tuple(int(p) for p in floor.split(".")) >= _NEWEST_FIRST_BOTOCORE, (name, package)


def test_a_real_logs_client_reads_newest_first():
    """The tests run the path production runs: a client from the pinned SDK
    passes the check. Creating it makes no call."""
    import boto3

    from routes import stacks

    assert stacks._reads_newest_first(boto3.client("logs", region_name="us-east-1"))


def test_the_last_minutes_of_a_busy_group_are_found_within_the_errors_tab_share():
    """3000 info lines in the last quarter hour and five errors 0.5-2 minutes
    ago, with the Errors tab's 10 calls per Lambda: read oldest first, the
    budget ran out at the start of the window and the tab showed none."""
    from routes import stacks

    info = [{"timestamp": ts, "message": f"[INFO] line {i}"} for i, ts in enumerate(spread(3000, 0.01, 14.9))]
    lines = info + _errors("recent", 5, 0.5, 2)
    fake = FakeLogs({GROUP: lines}, scan=200, newest_first=True)

    events = _read(fake, max_calls=min(stacks._call_shares(stacks._ERRORS_TAB_CALLS, 4))).events

    assert [e["message"] for e in reversed(events)] == newest(lines, 10, _is_error)
    assert len(events) == 5


def test_a_search_stops_at_its_deadline(monkeypatch):
    from routes import stacks

    clock = [1000.0]
    monkeypatch.setattr(stacks.time, "monotonic", lambda: clock[0])

    def slow_page(**kw):
        clock[0] += 3
        return {"events": [], "nextToken": "more"}

    read, logs = _run(slow_page, deadline=1000.0 + 8)

    assert read.events == []
    assert logs.filter_log_events.call_count == 3
    assert read.reason == stacks.UNREAD_BUDGET


def _stack(groups: int) -> dict:
    features = {"scheduler": True} if groups == 4 else {}
    return {"project": "bot", "environment": "dev", "features": features, "outputs": {"webhook_lambda": "dev-bot-webhook"}}


@pytest.mark.parametrize("lambdas", [3, 4])
def test_errors_tab_spends_one_budget_across_its_lambdas(monkeypatch, lambdas):
    """Each Lambda's search had its own 20 calls and they ran one after
    another: 80 calls for a busy stack, past the tab's 10 s timeout. The
    Lambdas now split the tab's 40 calls and use them all; with three, an
    even split rounded down left one unused."""
    from routes import stacks

    logs = MagicMock()
    logs.filter_log_events.side_effect = lambda **kw: {"events": [], "nextToken": "more"}
    monkeypatch.setattr(stacks, "logs", logs)
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: _stack(lambdas))

    resp = stacks.handle_get_errors({"id": "bot-dev"}, {})

    # Every Lambda ran out of calls with more to read: listed, but not a failure.
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["unreadable"] == [
        {"lambda": s, "reason": stacks.UNREAD_BUDGET} for s in stacks._lambda_suffixes(_stack(lambdas))
    ]
    groups = {c.kwargs["logGroupName"] for c in logs.filter_log_events.call_args_list}
    assert len(groups) == lambdas
    assert logs.filter_log_events.call_count == stacks._ERRORS_TAB_CALLS
