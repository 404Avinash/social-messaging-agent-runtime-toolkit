# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Log search and the Events feed show the newest lines in their window.

Both read each log group with one ``filter_log_events`` call. That API returns
events oldest first, so with a limit of 50 (log search) or 200 (Events) a busy
window filled the page with its oldest lines, and the messages a tester had just
sent were missing. In log search the Lambda platform's START/END/REPORT lines
also used up the limit before they were dropped. Both now search back from the
last 15 minutes like the Errors tab, and platform lines do not count.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from tests.unit.fake_logs import FakeLogs, ms as _ms, newest, spread

GROUP = "/aws/lambda/dev-bot-webhook"
EVENT_GROUP = "/aws/lambda/dev-bot-event"
CONFIG = {"project": "bot", "environment": "dev", "outputs": {"webhook_lambda": "dev-bot-webhook"}}


def _invocations(count: int, minutes: int) -> list[dict]:
    """``count`` webhook invocations over the last ``minutes``, six lines each."""
    now = datetime.now(UTC)
    events = []
    for i in range(count):
        t = _ms(now - timedelta(minutes=minutes) + timedelta(minutes=minutes) * i / count)
        for j, msg in enumerate(
            [
                f"START RequestId: r{i} Version: $LATEST",
                f"[INFO] inbound message {i}",
                f"[INFO] runtime replied {i}",
                f"[INFO] reply sent {i}",
                f"END RequestId: r{i}",
                f"REPORT RequestId: r{i} Duration: 900 ms",
            ]
        ):
            events.append({"timestamp": t + j, "message": msg})
    return events


def _burst(label: str, count: int, newest_minutes_ago: float, oldest_minutes_ago: float) -> list[dict]:
    """``count`` webhook invocations between the two ages, seven lines each."""
    events = []
    for i, t in enumerate(spread(count, newest_minutes_ago, oldest_minutes_ago)):
        for j, msg in enumerate(
            [
                f"START RequestId: {label}{i} Version: $LATEST",
                f"[INFO] {label} {i} inbound",
                f"[INFO] {label} {i} runtime invoked",
                f"[INFO] {label} {i} runtime replied",
                f"[INFO] {label} {i} reply sent",
                f"END RequestId: {label}{i}",
                f"REPORT RequestId: {label}{i} Duration: 900 ms",
            ]
        ):
            events.append({"timestamp": t + j, "message": msg})
    return events


def _application_line(event: dict) -> bool:
    return not event["message"].startswith(("START ", "END ", "REPORT "))


def _search(monkeypatch, fake, body):
    """The rows of a search that read every group in full."""
    from routes import logs

    monkeypatch.setattr(logs, "client", lambda *_: fake)
    monkeypatch.setattr(logs, "get_registry", lambda _id: CONFIG)
    resp = logs.search_logs_impl(body)
    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert answer["unreadable"] == []
    return answer["results"]


def test_log_search_returns_the_newest_invocation(monkeypatch):
    fake = FakeLogs({GROUP: _invocations(20, minutes=50)})

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 60})
    messages = [r["message"] for r in rows if r["logGroup"] == GROUP]

    assert "[INFO] inbound message 19" in messages
    assert messages[0] == "[INFO] reply sent 19"  # newest first


def test_log_search_does_not_spend_its_rows_on_platform_lines(monkeypatch):
    fake = FakeLogs({GROUP: _invocations(20, minutes=50)})

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 60})
    messages = [r["message"] for r in rows if r["logGroup"] == GROUP]

    assert not [m for m in messages if m.startswith(("START ", "END ", "REPORT "))]
    assert len(messages) == 50


def test_log_search_keeps_the_callers_filter_pattern(monkeypatch):
    fake = FakeLogs({GROUP: _invocations(20, minutes=50)})

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "inbound", "minutes": 60})

    assert {c.kwargs["filterPattern"] for c in fake.filter_log_events.call_args_list} == {"inbound"}
    assert [r["message"] for r in rows if r["logGroup"] == GROUP][0] == "[INFO] inbound message 19"


def test_log_search_merges_every_group_newest_first(monkeypatch):
    """The groups are read in parallel; the result is still one newest-first list."""
    notification = "/aws/lambda/dev-bot-notification"
    now = datetime.now(UTC)
    fake = FakeLogs({
        GROUP: _invocations(3, minutes=30),
        notification: [{"timestamp": _ms(now - timedelta(minutes=1)), "message": "[INFO] template sent"}],
    })

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 60})

    assert rows[0]["message"] == "[INFO] template sent" and rows[0]["lambda"] == "dev-bot-notification"
    assert len(rows) == 10
    assert [r["timestamp"] for r in rows] == sorted((r["timestamp"] for r in rows), reverse=True)


def test_log_search_stays_inside_the_requested_window(monkeypatch):
    fake = FakeLogs({GROUP: _invocations(4, minutes=5)})

    _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 5})

    for call in fake.filter_log_events.call_args_list:
        assert call.kwargs["endTime"] - call.kwargs["startTime"] <= 5 * 60_000 + 1000


def test_log_search_after_a_burst_shows_the_lines_since(monkeypatch):
    """A quiet last quarter hour after a burst: its lines, then the burst's newest.

    The hour holds more lines than the page bound reads, so reading the hour
    from its start returned the burst's oldest lines and none of the three
    messages sent since.
    """
    lines = _burst("burst", 100, newest_minutes_ago=25, oldest_minutes_ago=60) + _burst(
        "recent", 3, newest_minutes_ago=1, oldest_minutes_ago=5
    )
    fake = FakeLogs({GROUP: lines})

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 60})

    assert [r["message"] for r in rows] == newest(lines, 50, _application_line)
    assert rows[0]["message"] == "[INFO] recent 2 reply sent"


def test_log_search_in_a_busy_quarter_hour_shows_its_newest_lines(monkeypatch):
    lines = _burst("busy", 80, newest_minutes_ago=0.1, oldest_minutes_ago=10)
    fake = FakeLogs({GROUP: lines})

    rows = _search(monkeypatch, fake, {"stack": "bot-dev", "q": "", "minutes": 60})

    assert [r["message"] for r in rows] == newest(lines, 50, _application_line)
    assert rows[0]["message"] == "[INFO] busy 79 reply sent"


def _events_feed(monkeypatch, fake):
    """The events of a feed whose log group was read in full."""
    from routes import events, stacks

    monkeypatch.setattr(stacks, "logs", fake)
    monkeypatch.setattr(events, "_get_registry", lambda _id: CONFIG)
    resp = events.handle_get_events({"id": "bot-dev"}, {})
    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert answer["unreadable"] == []
    return answer["results"]


def test_events_feed_returns_the_newest_events(monkeypatch):
    now = datetime.now(UTC)
    lines = [
        {
            "timestamp": _ms(now - timedelta(hours=20) + timedelta(hours=20) * i / 300),
            "message": "SOCIALAGENT_EVENT " + json.dumps({"event_type": f"order-{i}", "source": "shop"}),
        }
        for i in range(300)
    ]
    fake = FakeLogs({EVENT_GROUP: lines})

    feed = _events_feed(monkeypatch, fake)

    assert feed[0]["eventType"] == "order-299"
    assert len(feed) == 200


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "FilterLogEvents")


@pytest.mark.parametrize("newest_first", [True, False])
def test_events_feed_is_empty_for_a_group_that_does_not_exist(monkeypatch, caplog, newest_first):
    # A Lambda's log group appears with its first invocation, so a stack
    # whose event Lambda never ran has none. That is no events, not a fault.
    fake = FakeLogs({}, newest_first=newest_first)
    fake.filter_log_events.side_effect = _client_error(
        "ResourceNotFoundException", "The specified log group does not exist."
    )
    caplog.set_level(logging.DEBUG)

    assert _events_feed(monkeypatch, fake) == []
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("newest_first", [True, False])
def test_a_refused_filter_pattern_is_a_400_that_never_quotes_it(monkeypatch, caplog, newest_first):
    """It answered 200 with no rows, so a pattern CloudWatch Logs refused
    read as a search that found nothing. A CloudWatch Logs error message can
    quote the filter pattern, which is what the operator searched for, a
    phone number included, so neither the answer nor the log repeats it."""
    from routes import logs

    pattern = "525555550100"
    fake = FakeLogs({GROUP: []}, newest_first=newest_first)
    fake.filter_log_events.side_effect = _client_error(
        "InvalidParameterException", f"Invalid filter pattern: {pattern}"
    )
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logs, "client", lambda *_: fake)
    monkeypatch.setattr(logs, "get_registry", lambda _id: CONFIG)

    resp = logs.search_logs_impl({"stack": "bot-dev", "q": pattern, "minutes": 60})

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"]) == {"error": "Invalid filter pattern"}
    assert pattern not in resp["body"]
    logged = {(r.getMessage(), getattr(r, "error_code", None), r.levelno) for r in caplog.records}
    assert logged == {("filter_log_events_failed", "InvalidParameterException", logging.WARNING)}
    for record in caplog.records:
        assert record.exc_info is None
        assert pattern not in str(record.__dict__)


def test_events_feed_after_a_busy_afternoon_keeps_the_recent_events(monkeypatch):
    def event(name: str, ts: int) -> dict:
        body = json.dumps({"event_type": name, "source": "shop"})
        return {"timestamp": ts, "message": f"SOCIALAGENT_EVENT {body}"}

    older = [event(f"old-{i}", ts) for i, ts in enumerate(spread(600, 120, 360))]
    recent = [event(f"new-{i}", ts) for i, ts in enumerate(spread(20, 1, 30))]
    fake = FakeLogs({EVENT_GROUP: older + recent})

    feed = _events_feed(monkeypatch, fake)

    assert [e["eventType"] for e in feed] == [f"new-{i}" for i in range(19, -1, -1)] + [
        f"old-{i}" for i in range(599, 419, -1)
    ]


def test_log_search_spends_one_budget_across_every_group(monkeypatch):
    """Each group's search had its own 20 calls: 480 for six busy stacks,
    against an account-wide FilterLogEvents quota of 10 a second in most
    Regions, past the backend's 30 s timeout."""
    from unittest.mock import MagicMock

    from routes import logs

    registries = {
        f"bot{i}-dev": {"project": f"bot{i}", "environment": "dev", "features": {"scheduler": True},
                        "outputs": {"webhook_lambda": f"dev-bot{i}-webhook"}}
        for i in range(6)
    }
    reader = MagicMock()
    reader.filter_log_events.side_effect = lambda **kw: {"events": [], "nextToken": "more"}
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    resp = logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    assert resp["statusCode"] == 200
    groups = {c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list}
    assert len(groups) == 24
    assert reader.filter_log_events.call_count <= logs._LOG_SEARCH_CALLS


def _stack_mix(with_scheduler: int, without: int) -> dict[str, dict]:
    """Registry entries: ``with_scheduler`` stacks with the opt-in scheduler
    Lambda (four log groups each), then ``without`` stacks without it (three)."""
    return {
        f"bot{i:02d}-dev": {"project": f"bot{i:02d}", "environment": "dev",
                            "features": {"scheduler": True} if i < with_scheduler else {},
                            "outputs": {"webhook_lambda": f"dev-bot{i:02d}-webhook"}}
        for i in range(with_scheduler + without)
    }


def _registries(stacks: int, scheduler: bool) -> dict[str, dict]:
    """``stacks`` registry entries, with the opt-in scheduler Lambda or not."""
    return _stack_mix(stacks, 0) if scheduler else _stack_mix(0, stacks)


def _group_names(registries: dict[str, dict]) -> list[str]:
    """Every log group a search over ``registries`` reads, in registry order."""
    from routes.stacks import _lambda_suffixes

    return [
        f"/aws/lambda/{cfg['outputs']['webhook_lambda'].rsplit('-webhook', 1)[0]}-{suffix}"
        for cfg in registries.values()
        for suffix in _lambda_suffixes(cfg)
    ]


def _endless_reader(newest_first: bool, pause: float = 0.0, full: bool = False):
    """A busy log group with no matches: every page is empty with more to read.

    ``newest_first`` gives the reader the ``startFromHead`` parameter, so both
    the newest-first path and the window-by-window path are exercised. With
    ``full``, every page is full of the platform's REPORT lines instead, which
    log search drops as read: the window-by-window path then halves each range
    rather than follow its page. The reader also records the most calls it
    saw in flight at once.
    """
    import threading
    import time
    from unittest.mock import MagicMock

    from routes.stacks import _PAGE_LIMIT

    report = {"timestamp": 1, "message": "REPORT RequestId: r0 Duration: 900 ms"}
    reader = MagicMock()
    if newest_first:
        reader.meta.service_model.operation_model.return_value.input_shape.members = {"startFromHead": None}
    else:
        reader.meta.service_model.operation_model.return_value.input_shape.members = {}
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def page(**_):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(pause)
        with lock:
            state["now"] -= 1
        return {"events": [report] * _PAGE_LIMIT if full else [], "nextToken": "more"}

    reader.filter_log_events.side_effect = page
    reader.peak = lambda: state["peak"]
    return reader


# More than six stacks: 7 with the scheduler Lambda is 28 log groups, 9
# without is 27, 12 with is 48 and 30 without is 90. Each group had a floor of
# two calls of its own, so these started 56, 54, 96 and 180 calls.
_MANY_STACKS = [(7, True), (9, False), (12, True), (30, False)]


@pytest.mark.parametrize("newest_first", [True, False], ids=["newest-first", "window"])
@pytest.mark.parametrize(("stacks", "scheduler"), _MANY_STACKS, ids=["7x4", "9x3", "12x4", "30x3"])
def test_log_search_never_starts_more_than_the_request_budget(monkeypatch, stacks, scheduler, newest_first):
    from routes import logs

    registries = _registries(stacks, scheduler)
    reader = _endless_reader(newest_first)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    resp = logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    assert resp["statusCode"] == 200
    assert reader.filter_log_events.call_count <= logs._LOG_SEARCH_CALLS


@pytest.mark.parametrize("newest_first", [True, False], ids=["newest-first", "window"])
@pytest.mark.parametrize(("stacks", "scheduler"), [(7, True), (12, True)], ids=["7x4", "12x4"])
def test_log_search_reads_every_group_while_the_budget_covers_one_call_each(
    monkeypatch, stacks, scheduler, newest_first
):
    """Up to 48 groups, the cap takes no group's only call."""
    from routes import logs

    registries = _registries(stacks, scheduler)
    reader = _endless_reader(newest_first)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    read = {c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list}
    assert read == set(_group_names(registries))


# (stacks with the scheduler Lambda, stacks without it), and the calls each
# log group makes in registry order, as runs of (groups, calls each). An even
# split rounded down gave each of 25 to 48 groups one call: 25 groups used 25
# of the 48 calls, 28 used 28, 27 used 27, and 7 groups 42.
_SPLITS = {
    "1x3": ((0, 1), [(3, 16)]),
    "1x4+1x3": ((1, 1), [(6, 7), (1, 6)]),
    "6x4": ((6, 0), [(24, 2)]),
    "4x4+3x3": ((4, 3), [(23, 2), (2, 1)]),
    "9x3": ((0, 9), [(21, 2), (6, 1)]),
    "7x4": ((7, 0), [(20, 2), (8, 1)]),
    "12x4": ((12, 0), [(48, 1)]),
    "30x3": ((0, 30), [(48, 1), (42, 0)]),
}


@pytest.mark.parametrize("full", [False, True], ids=["empty-pages", "full-pages"])
@pytest.mark.parametrize("newest_first", [True, False], ids=["newest-first", "window"])
@pytest.mark.parametrize(("mix", "runs"), list(_SPLITS.values()), ids=list(_SPLITS))
def test_log_search_spends_the_whole_budget_and_gives_what_is_left_to_the_first_groups(
    monkeypatch, mix, runs, newest_first, full
):
    """Every group has more to read, so each spends its whole share: the
    shares add up to 48, and the calls an even split leaves over go to the
    first groups in registry order."""
    from collections import Counter

    from routes import logs

    registries = _stack_mix(*mix)
    reader = _endless_reader(newest_first, full=full)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    resp = logs.search_logs_impl({"q": "", "minutes": 60})

    assert resp["statusCode"] == 200
    calls = Counter(c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list)
    assert [calls[name] for name in _group_names(registries)] == [each for groups, each in runs for _ in range(groups)]
    assert reader.filter_log_events.call_count == logs._LOG_SEARCH_CALLS


def test_log_search_past_the_budget_reads_the_first_groups_and_logs_the_rest(monkeypatch, caplog):
    """90 groups and 48 calls: the first 48 in registry order are read, once
    each, and the search logs how many it did not read. Every page has more
    to read, so the answer lists all 90 for the budget: the 42 it left out
    and the 48 whose one call was not enough."""
    from routes import logs, stacks

    registries = _registries(30, scheduler=False)
    reader = _endless_reader(newest_first=True)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)
    caplog.set_level(logging.INFO)

    resp = logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    assert resp["statusCode"] == 200
    called = [c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list]
    assert sorted(called) == sorted(_group_names(registries)[: logs._LOG_SEARCH_CALLS])
    incomplete = [r for r in caplog.records if r.getMessage() == "log_search_incomplete"]
    assert [(r.levelno, r.log_groups, r.not_read) for r in incomplete] == [(logging.WARNING, 90, 42)]
    assert json.loads(resp["body"])["unreadable"] == [
        {"lambda": name.split("/")[-1], "reason": stacks.UNREAD_BUDGET} for name in _group_names(registries)
    ]


def test_log_search_past_the_budget_reads_the_first_groups_when_they_start_last(monkeypatch):
    """90 groups, so each gets one call, and the first stack's three searches
    start a quarter second late, as a worker thread can. Groups past the
    48th are left out before any worker starts, so the first 48 in registry
    order are still the ones read. Without that cut, the 48 groups after the
    late ones took the calls first, three of them past the 48th, so which
    groups a search read depended on how its threads were scheduled. A slow
    answer alone does not show this: a group spends its call before it waits
    for the answer."""
    import time
    from collections import Counter

    from routes import logs

    registries = _registries(30, scheduler=False)
    names = _group_names(registries)
    late = set(names[:3])
    reader = _endless_reader(newest_first=True)
    search = logs._newest_log_events

    def late_for_the_first_stack(log_group, *args, **kwargs):
        if log_group in late:
            time.sleep(0.25)
        return search(log_group, *args, **kwargs)

    monkeypatch.setattr(logs, "_newest_log_events", late_for_the_first_stack)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    called = Counter(c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list)
    assert called == Counter(names[: logs._LOG_SEARCH_CALLS])


def test_log_search_counts_a_group_the_deadline_stops_before_its_first_call(monkeypatch, caplog):
    """Nine groups on eight workers. The deadline passes while the first
    eight are reading, so the ninth, within the 48, starts no call: it is
    counted with the groups not read, and listed in the answer for the
    budget, and the eight that were read are not."""
    import threading
    from unittest.mock import MagicMock

    from routes import logs, stacks

    registries = _registries(3, scheduler=False)
    names = _group_names(registries)
    clock = [1000.0]
    monkeypatch.setattr(logs.time, "monotonic", lambda: clock[0])
    reading = threading.Barrier(8, timeout=10)
    reader = MagicMock()
    reader.meta.service_model.operation_model.return_value.input_shape.members = {"startFromHead": None}

    def page(**_):
        reading.wait()  # every worker has started its group's first call
        clock[0] = 1000.0 + logs._LOG_SEARCH_SECONDS
        return {"events": []}

    reader.filter_log_events.side_effect = page
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)
    caplog.set_level(logging.INFO)

    resp = logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    assert resp["statusCode"] == 200
    called = [c.kwargs["logGroupName"] for c in reader.filter_log_events.call_args_list]
    assert sorted(called) == sorted(names[:8])
    incomplete = [r for r in caplog.records if r.getMessage() == "log_search_incomplete"]
    assert [(r.levelno, r.log_groups, r.not_read) for r in incomplete] == [(logging.WARNING, 9, 1)]
    assert json.loads(resp["body"])["unreadable"] == [{"lambda": names[8].split("/")[-1], "reason": stacks.UNREAD_BUDGET}]


def test_log_search_under_concurrency_keeps_the_budget_and_eight_workers(monkeypatch):
    """Slow calls overlap across the workers; the total and the fan-out still hold."""
    from routes import logs

    registries = _registries(12, scheduler=True)
    reader = _endless_reader(newest_first=True, pause=0.02)
    monkeypatch.setattr(logs, "client", lambda *_: reader)
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)

    logs.search_logs_impl({"q": "ERROR", "minutes": 60})

    assert reader.filter_log_events.call_count <= logs._LOG_SEARCH_CALLS
    assert 1 < reader.peak() <= 8


def test_the_call_budget_grants_exactly_its_calls_across_threads():
    """Eight groups' shares spend one request budget from eight threads.

    A check-then-decrement without the lock granted 50 of 48 calls. The race
    is only at the last calls, so many fresh budgets are spent, each by
    threads that start together, with a short switch interval to interleave
    them.
    """
    import sys
    import threading

    from routes.stacks import _CallBudget

    def spend(budget: _CallBudget, start: threading.Barrier, granted: list[int]) -> None:
        share = budget.share(48)
        start.wait()
        while share.take():
            pass
        granted.append(share.taken)

    rounds = []
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(100):
            budget = _CallBudget(48, deadline=float("inf"))
            start = threading.Barrier(8)
            granted: list[int] = []
            threads = [threading.Thread(target=spend, args=(budget, start, granted)) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            rounds.append(sum(granted))
            assert not budget.has_left()
    finally:
        sys.setswitchinterval(interval)

    assert set(rounds) == {48}


def test_a_share_stops_at_its_own_cap_and_at_the_request_budget():
    from routes.stacks import _CallBudget

    budget = _CallBudget(5, deadline=float("inf"))
    first = budget.share(3)
    assert [first.take() for _ in range(4)] == [True, True, True, False]
    assert first.taken == 3 and not first.has_left()

    second = budget.share(3)
    assert [second.take() for _ in range(3)] == [True, True, False]
    assert second.taken == 2 and not second.has_left() and not budget.has_left()


def test_a_share_starts_no_call_past_the_request_deadline(monkeypatch):
    from routes import stacks

    clock = [1000.0]
    monkeypatch.setattr(stacks.time, "monotonic", lambda: clock[0])
    share = stacks._CallBudget(48, deadline=1005.0).share(20)

    assert share.take()
    clock[0] = 1005.0
    assert not share.has_left() and not share.take()
    assert share.taken == 1


class _RefusesAfter:
    """A budget that says it has calls left but grants only the first ``calls``,
    as a request-wide budget does when another group's search takes the last
    call between this search's check and its next call."""

    def __init__(self, calls: int):
        self.calls = calls

    def has_left(self) -> bool:
        return True

    def take(self) -> bool:
        self.calls -= 1
        return self.calls >= 0


def test_a_range_halved_when_the_budget_runs_out_keeps_the_rows_it_read():
    """The window path halves a range whose page comes back full. When the
    next call is refused, the search shows the newest rows of the page it
    read before the halving, not nothing."""
    from unittest.mock import MagicMock

    from routes import stacks

    page = [{"timestamp": 1_000 + i, "message": f"[ERROR] line {i}"} for i in range(stacks._PAGE_LIMIT)]
    reader = MagicMock()
    reader.meta.service_model.operation_model.return_value.input_shape.members = {}
    reader.filter_log_events.return_value = {"events": page, "nextToken": "more"}

    read = stacks._newest_log_events(GROUP, "ERROR", hours=1, client=reader, budget=_RefusesAfter(1))

    assert reader.filter_log_events.call_count == 1
    assert [e["message"] for e in read.events] == [f"[ERROR] line {i}" for i in range(90, 100)]
    assert read.reason == stacks.UNREAD_BUDGET
