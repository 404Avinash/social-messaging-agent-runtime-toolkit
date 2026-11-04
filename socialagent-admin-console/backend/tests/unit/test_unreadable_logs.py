# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A log group the console could not read is reported, never shown as empty.

Log search, the Errors tab and the Events feed turned every failed
``filter_log_events`` call (access denied, throttled, a refused filter
pattern) into no rows and a 200. The Logs page, the Overview's and Stack
Detail's Recent errors, the Errors tab and the Events page then said there
were no errors or no events for logs nobody had read. Each route now answers
``{results, unreadable}``: the rows it read, and each Lambda whose log group
it could not read in full with the reason. With nothing read and a call
failed, it answers 502. FilterLogEvents' quota is account-wide, so a
throttled group among readable ones is the usual case, and those rows are
kept.
"""

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from tests.unit.fake_logs import FakeLogs, ms

PREFIX = "dev-bot"
CONFIG = {"project": "bot", "environment": "dev", "outputs": {"webhook_lambda": f"{PREFIX}-webhook"}}
SUFFIXES = ["webhook", "notification", "event"]
# What an operator may search for: a user's number.
PHONE_PATTERN = "525555550100"
PATHS = pytest.mark.parametrize("newest_first", [True, False], ids=["newest-first", "window"])


def _group(suffix: str) -> str:
    return f"/aws/lambda/{PREFIX}-{suffix}"


def _client_error(code: str) -> ClientError:
    # The message quotes the pattern, as a CloudWatch Logs error can.
    return ClientError({"Error": {"Code": code, "Message": f"failed on {PHONE_PATTERN}"}}, "FilterLogEvents")


def _recent(message: str, minutes_ago: float = 1) -> dict:
    return {"timestamp": ms(datetime.now(UTC) - timedelta(minutes=minutes_ago)), "message": message}


def _logs(groups: dict, newest_first: bool, fail: dict | None = None, after: int = 0, scan: int | None = None):
    """FakeLogs whose calls on each group in ``fail`` raise its error once
    ``after`` calls on that group have been answered."""
    fake = FakeLogs(groups, scan=scan, newest_first=newest_first)
    answered: Counter = Counter()
    fail = fail or {}

    def filter_log_events(**kw):
        group = kw["logGroupName"]
        if group in fail and answered[group] >= after:
            raise fail[group]
        answered[group] += 1
        return fake._filter(**kw)

    fake.filter_log_events.side_effect = filter_log_events
    return fake


def _search(monkeypatch, fake, q: str = "ERROR", registries: dict | None = None) -> dict:
    from routes import logs

    monkeypatch.setattr(logs, "client", lambda *_: fake)
    if registries is None:
        monkeypatch.setattr(logs, "get_registry", lambda _id: CONFIG)
        return logs.search_logs_impl({"stack": "bot-dev", "q": q, "minutes": 60})
    monkeypatch.setattr(logs, "get_all_registries", lambda: registries)
    return logs.search_logs_impl({"q": q, "minutes": 60})


def _errors_tab(monkeypatch, fake) -> dict:
    from routes import stacks

    monkeypatch.setattr(stacks, "logs", fake)
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: CONFIG)
    return stacks.handle_get_errors({"id": "bot-dev"}, {})


def _events_feed(monkeypatch, fake) -> dict:
    from routes import events, stacks

    monkeypatch.setattr(stacks, "logs", fake)
    monkeypatch.setattr(events, "_get_registry", lambda _id: CONFIG)
    return events.handle_get_events({"id": "bot-dev"}, {})


# Each route, and how it names a Lambda in ``unreadable``: as its rows do.
# Log search spans stacks, so it names the function; the Errors tab names the
# suffix its Lambda column shows; the Events feed reads only the event Lambda.
ROUTES = {
    "log-search": (_search, {s: f"{PREFIX}-{s}" for s in SUFFIXES}),
    "errors-tab": (_errors_tab, {s: s for s in SUFFIXES}),
    "events": (_events_feed, {"event": f"{PREFIX}-event"}),
}
EACH_ROUTE = pytest.mark.parametrize("route", list(ROUTES))


@pytest.fixture
def request_id(monkeypatch):
    from shared import http

    monkeypatch.setattr(http, "request_id", lambda: "req-123")


@EACH_ROUTE
@PATHS
@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException"])
def test_a_route_that_could_read_no_log_group_answers_502_naming_each(
    monkeypatch, caplog, request_id, route, newest_first, code
):
    call, names = ROUTES[route]
    fake = _logs({}, newest_first, fail={_group(s): _client_error(code) for s in SUFFIXES})
    caplog.set_level(logging.DEBUG)

    resp = _search(monkeypatch, fake, q=PHONE_PATTERN) if route == "log-search" else call(monkeypatch, fake)

    assert resp["statusCode"] == 502
    assert json.loads(resp["body"]) == {
        "error": "Could not read any log group (request id req-123)",
        "unreadable": [{"lambda": name, "reason": code} for name in names.values()],
        "requestId": "req-123",
    }
    # The error message quotes the pattern; neither the answer nor the log does.
    assert PHONE_PATTERN not in resp["body"]
    for record in caplog.records:
        assert PHONE_PATTERN not in str(record.__dict__)


@pytest.mark.parametrize("route", ["log-search", "errors-tab"])
@PATHS
def test_a_throttled_group_is_listed_beside_the_rows_the_others_gave(monkeypatch, route, newest_first):
    call, names = ROUTES[route]
    fake = _logs(
        {_group("webhook"): [_recent("[ERROR] reply failed")]},
        newest_first,
        fail={_group("notification"): _client_error("ThrottlingException")},
    )

    resp = call(monkeypatch, fake)

    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert [r["message"] for r in answer["results"]] == ["[ERROR] reply failed"]
    assert answer["unreadable"] == [{"lambda": names["notification"], "reason": "ThrottlingException"}]


@pytest.mark.parametrize("route", ["log-search", "errors-tab"])
@PATHS
def test_a_group_read_without_a_match_beside_a_failed_one_is_no_failure(monkeypatch, route, newest_first):
    """Two groups read and empty, one refused: nothing to show, but the read
    worked, so it is a 200 that names the one group, not a 502."""
    call, names = ROUTES[route]
    fake = _logs({}, newest_first, fail={_group("event"): _client_error("AccessDeniedException")})

    resp = call(monkeypatch, fake)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {
        "results": [],
        "unreadable": [{"lambda": names["event"], "reason": "AccessDeniedException"}],
    }


# Each route's log line for a fault in the code that builds a group's rows.
FAULT_EVENTS = {"log-search": "log_search_group_failed", "errors-tab": "errors_tab_read_failed"}


@pytest.mark.parametrize("route", list(FAULT_EVENTS))
@PATHS
def test_a_group_whose_rows_cannot_be_built_is_listed_by_the_fault(monkeypatch, caplog, route, newest_first):
    """The read worked, but the code that turns the webhook's events into rows
    raised on one without a timestamp. That group gave no rows, so it is
    listed with the exception's type, where it was dropped as if it held
    nothing. The other groups' rows are still answered."""
    call, names = ROUTES[route]
    fake = FakeLogs({_group("notification"): [_recent("[ERROR] reply failed")]}, newest_first=newest_first)

    def filter_log_events(**kw):
        if kw["logGroupName"] == _group("webhook"):
            return {"events": [{"message": "[ERROR] no timestamp"}]}
        return fake._filter(**kw)

    fake.filter_log_events.side_effect = filter_log_events
    caplog.set_level(logging.DEBUG)

    resp = call(monkeypatch, fake)

    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert [r["message"] for r in answer["results"]] == ["[ERROR] reply failed"]
    assert answer["unreadable"] == [{"lambda": names["webhook"], "reason": "KeyError"}]
    # A fault in this code, not a CloudWatch Logs error, so it keeps its traceback.
    [record] = [r for r in caplog.records if r.getMessage() == FAULT_EVENTS[route]]
    assert isinstance(record.exc_info[1], KeyError)


def _many_lines(count: int, message=lambda i: f"[ERROR] line {i}") -> list[dict]:
    """``count`` lines spread over the last 10 minutes, oldest first."""
    return [_recent(message(i), minutes_ago=10 - 9 * i / count) for i in range(count)]


@PATHS
def test_a_search_that_fails_after_a_page_keeps_its_rows_and_lists_the_group(monkeypatch, newest_first):
    """Each page scans 30 lines, so the group needs more than one call; the
    second is throttled."""
    lines = _many_lines(200)
    fake = _logs(
        {_group("webhook"): lines}, newest_first, scan=30,
        fail={_group("webhook"): _client_error("ThrottlingException")}, after=1,
    )

    resp = _search(monkeypatch, fake, q="ERROR")

    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert answer["results"] and all(r["lambda"] == f"{PREFIX}-webhook" for r in answer["results"])
    assert answer["unreadable"] == [{"lambda": f"{PREFIX}-webhook", "reason": "ThrottlingException"}]


@PATHS
def test_an_events_feed_that_fails_after_a_page_keeps_its_events(monkeypatch, newest_first):
    lines = _many_lines(300, lambda i: "SOCIALAGENT_EVENT " + json.dumps({"event_type": "order", "summary": str(i)}))
    fake = _logs(
        {_group("event"): lines}, newest_first, scan=50,
        fail={_group("event"): _client_error("ThrottlingException")}, after=1,
    )

    resp = _events_feed(monkeypatch, fake)

    assert resp["statusCode"] == 200
    answer = json.loads(resp["body"])
    assert answer["results"] and {e["eventType"] for e in answer["results"]} == {"order"}
    assert answer["unreadable"] == [{"lambda": f"{PREFIX}-event", "reason": "ThrottlingException"}]


@EACH_ROUTE
@PATHS
def test_log_groups_that_do_not_exist_yet_are_no_events_not_unreadable(monkeypatch, caplog, route, newest_first):
    # A Lambda's log group appears with its first invocation.
    call, _ = ROUTES[route]
    missing = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "The specified log group does not exist."}},
        "FilterLogEvents",
    )
    fake = _logs({}, newest_first, fail={_group(s): missing for s in SUFFIXES})
    caplog.set_level(logging.DEBUG)

    resp = call(monkeypatch, fake)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"results": [], "unreadable": []}
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_an_sdk_error_without_a_code_is_named_by_its_type(monkeypatch):
    fake = _logs(
        {}, newest_first=True,
        fail={_group("webhook"): EndpointConnectionError(endpoint_url="https://logs.us-east-1.amazonaws.com/")},
    )

    resp = _search(monkeypatch, fake)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["unreadable"] == [
        {"lambda": f"{PREFIX}-webhook", "reason": "EndpointConnectionError"}
    ]


def _endless(newest_first: bool):
    """Every page is empty with more to read: a busy group with no match."""
    fake = FakeLogs({}, newest_first=newest_first)
    fake.filter_log_events.side_effect = lambda **_: {"events": [], "nextToken": "more"}
    return fake


@PATHS
def test_an_events_feed_the_budget_cut_short_is_listed_but_not_a_failure(monkeypatch, newest_first):
    from routes import stacks

    resp = _events_feed(monkeypatch, _endless(newest_first))

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {
        "results": [],
        "unreadable": [{"lambda": f"{PREFIX}-event", "reason": stacks.UNREAD_BUDGET}],
    }


def test_a_search_past_the_budget_lists_the_groups_it_left_out(monkeypatch):
    """90 log groups and 48 calls. Every group called is refused and the
    other 42 make no call, so nothing was read: a 502 that names all 90, the
    42 for the budget."""
    from routes import stacks
    from tests.unit.test_log_search_newest import _group_names, _registries

    registries = _registries(30, scheduler=False)
    names = _group_names(registries)
    fake = _logs({}, newest_first=True, fail={g: _client_error("AccessDeniedException") for g in names})

    resp = _search(monkeypatch, fake, registries=registries)

    assert resp["statusCode"] == 502
    unreadable = json.loads(resp["body"])["unreadable"]
    assert unreadable == [{"lambda": g.split("/")[-1], "reason": "AccessDeniedException"} for g in names[:48]] + [
        {"lambda": g.split("/")[-1], "reason": stacks.UNREAD_BUDGET} for g in names[48:]
    ]


def test_an_empty_pattern_refused_as_invalid_is_a_failure_not_a_bad_pattern(monkeypatch):
    """Without a pattern the operator wrote, an invalid parameter is not
    theirs to fix, so it is not answered as one."""
    fake = _logs({}, newest_first=True, fail={_group(s): _client_error("InvalidParameterException") for s in SUFFIXES})

    resp = _search(monkeypatch, fake, q="")

    assert resp["statusCode"] == 502
    assert {u["reason"] for u in json.loads(resp["body"])["unreadable"]} == {"InvalidParameterException"}


@pytest.mark.parametrize(("minutes", "window"), [(-5, 60), (0, 60), (10_000, 1440)])
def test_a_window_cloudwatch_would_refuse_is_never_asked_for(monkeypatch, minutes, window):
    """A window that ends before it starts is an invalid parameter, which the
    route would answer as an invalid filter pattern."""
    from routes import logs

    fake = FakeLogs({}, newest_first=True)
    monkeypatch.setattr(logs, "client", lambda *_: fake)
    monkeypatch.setattr(logs, "get_registry", lambda _id: CONFIG)

    logs.search_logs_impl({"stack": "bot-dev", "q": "ERROR", "minutes": minutes})

    for call in fake.filter_log_events.call_args_list:
        assert call.kwargs["endTime"] - call.kwargs["startTime"] == pytest.approx(window * 60_000, abs=1000)


def test_a_busy_range_with_no_call_left_to_halve_it_is_not_read_in_full():
    """The window path's first page comes back full with more to read, and
    the budget has no call left to halve the range. Its rows are the range's
    oldest, so the search says it stopped short."""
    from unittest.mock import MagicMock

    from routes import stacks

    page = [{"timestamp": 1_000 + i, "message": f"[ERROR] line {i}"} for i in range(stacks._PAGE_LIMIT)]
    reader = MagicMock()
    reader.meta.service_model.operation_model.return_value.input_shape.members = {}
    reader.filter_log_events.return_value = {"events": page, "nextToken": "more"}

    read = stacks._newest_log_events(_group("webhook"), "ERROR", hours=1, client=reader, max_calls=1)

    assert reader.filter_log_events.call_count == 1
    assert len(read.events) == 10
    assert read.reason == stacks.UNREAD_BUDGET


@PATHS
def test_a_group_read_to_the_end_of_its_window_is_complete(monkeypatch, newest_first):
    """Fewer matches than asked for, and every page read: nothing to report."""
    from routes import stacks

    fake = FakeLogs({_group("webhook"): [_recent("[ERROR] one"), _recent("[ERROR] two", 2)]}, newest_first=newest_first)

    read = stacks._newest_log_events(_group("webhook"), "ERROR", hours=24, client=fake)

    assert [e["message"] for e in read.events] == ["[ERROR] two", "[ERROR] one"]
    assert read.reason is None


def test_the_last_range_the_budget_stops_between_pages_is_not_read_in_full():
    """The window path follows a sparse range's partial pages from its oldest
    end, and the budget runs out before the last page. The range is the
    search's only one, so no later range's check notices the budget is gone:
    the page loop has to say the read stopped short, or the oldest few
    matches read as the whole window."""
    from routes import stacks

    lines = [_recent("[ERROR] line" if i % 5 == 0 else "INFO line", minutes_ago=10 - i / 10) for i in range(100)]
    fake = FakeLogs({_group("webhook"): lines}, scan=10)

    read = stacks._newest_log_events(_group("webhook"), "ERROR", hours=0.25, client=fake, max_calls=2)

    assert fake.filter_log_events.call_count == 2
    assert len(read.events) == 4
    assert read.reason == stacks.UNREAD_BUDGET


def test_a_range_left_at_its_page_bound_is_not_read_in_full():
    """A range under two seconds cannot be halved, so the window path reads it
    for up to five pages, from its oldest end. A sixth page was left unread
    without a word, so its newest lines were missing from a read that looked
    complete."""
    from routes import stacks

    now = datetime.now(UTC)
    lines = [{"timestamp": ms(now) - 1500 + i, "message": f"[ERROR] line {i}"} for i in range(100)]
    fake = FakeLogs({_group("webhook"): lines}, scan=10)

    read = stacks._newest_log_events(_group("webhook"), "ERROR", hours=0.0005, keep=100, client=fake)

    assert fake.filter_log_events.call_count == 5
    assert len(read.events) == 50
    assert read.reason == stacks.UNREAD_BUDGET
