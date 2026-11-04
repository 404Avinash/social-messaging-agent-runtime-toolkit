# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The template routes must say what they did not check.

To find the templates Meta reclassified, the list and health routes read each
matching template's detail, one GetWhatsAppMessageTemplate call at a time, at
most 10 a second per account and Region, inside a console API that stops at
30 s. A few hundred templates outlasted the request, and the console then hid
the stack, or every stack's banner, without saying so. The routes now start no
EUM Social call of any kind past a deadline, list every template before they
read any, and bound each call with short timeouts and one retry, so their EUM
Social calls end inside 30 s. They report a template they did not reach, or
could not read, as not checked, never as not reclassified; health names the
stacks it could not list, and the list route fails rather than list part of a
stack.
"""
import json
import logging
import re
from pathlib import Path
from unittest.mock import call, patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

# Route modules read env vars at import time, set by conftest's autouse aws_env
# fixture, so import inside each test (as tests/unit/test_input_validation.py does).

MOVED = {"category": "MARKETING", "previous_category": "UTILITY"}

# The HTTP API in front of the console backend stops a request at 30 s, the
# most an HTTP API integration may take, and the backend Lambda at the
# timeout API_MODULE sets. The routes leave 3 s of the shorter for a cold
# start, the caller's group lookup and the response; the registry read comes
# out of the budget.
HTTP_API_LIMIT_SECONDS = 30
OUTSIDE_EUM_SECONDS = 3
API_MODULE = Path(__file__).resolve().parents[3] / "infra-terraform" / "modules" / "api" / "main.tf"


def _budget():
    from routes import templates

    return templates._EUM_BUDGET_SECONDS


class _Clock:
    """Stands in for the route module's ``time``: only the stubs move it.

    ``calls`` records every EUM Social call the stubs see, as
    ``(operation, time it started)``.
    """

    def __init__(self):
        self.now = 1000.0
        self.calls = []

    def monotonic(self):
        return self.now

    def started(self, operation):
        self.calls.append((operation, self.now))

    def operations(self):
        return [operation for operation, _ in self.calls]

    def assert_no_call_after(self, deadline):
        late = [(operation, at) for operation, at in self.calls if at >= deadline]
        assert late == [], f"EUM Social calls started at or past the deadline {deadline}: {late}"


def _stack(project, phone=True):
    """Registry entry whose templates are named ``dev_<project>_...``."""
    config = {
        "project": project,
        "environment": "dev",
        "region": "us-east-1",
        "template_prefix": f"dev_{project}_",
    }
    if phone:
        config["phone_number_id"] = f"phone-{project}"
    return config


def _listed(name, status):
    return {
        "templateName": name,
        "metaTemplateId": f"meta-{name}",
        "templateStatus": status,
        "templateCategory": "UTILITY",
        "templateLanguage": "en_US",
    }


def _eum(
    b3,
    clock,
    *,
    phones,
    pages,
    details=None,
    statuses=None,
    accounts=None,
    detail_seconds=0.0,
    page_seconds=0.0,
    phone_seconds=0.0,
    account_seconds=0.0,
):
    """Stub EUM Social, one client for every stack.

    *phones* maps a phone number id to its WABA id, or to an exception its
    lookup raises. *pages* maps a WABA id to its listing pages, each a list of
    template names or an exception that page raises. *details* maps a template
    name to its detail (default ``{}``) or to an exception its call raises, and
    *statuses* to its approval status (default ``APPROVED``). *accounts* lists
    the pages of linked WABA ids the WABA fallback reads. Each detail call
    moves *clock* on by *detail_seconds*, each listing page by
    *page_seconds*, each phone lookup by *phone_seconds* and each page of
    linked accounts by *account_seconds*. Every call is recorded on *clock*.
    """
    details = details or {}
    statuses = statuses or {}
    accounts = accounts or [[]]
    eum = b3.client.return_value

    def phone_lookup(id):
        clock.started("phone")
        clock.now += phone_seconds
        waba_id = phones[id]
        if isinstance(waba_id, Exception):
            raise waba_id
        return {"linkedWhatsAppBusinessAccountId": waba_id}

    def list_accounts(maxResults, nextToken=None):
        clock.started("accounts")
        clock.now += account_seconds
        index = int(nextToken or 0)
        resp = {"linkedAccounts": [{"id": waba_id} for waba_id in accounts[index]]}
        if index + 1 < len(accounts):
            resp["nextToken"] = str(index + 1)
        return resp

    def list_templates(id, maxResults, nextToken=None):
        clock.started("page")
        clock.now += page_seconds
        index = int(nextToken or 0)
        page = pages[id][index]
        if isinstance(page, Exception):
            raise page
        resp = {"templates": [_listed(name, statuses.get(name, "APPROVED")) for name in page]}
        if index + 1 < len(pages[id]):
            resp["nextToken"] = str(index + 1)
        return resp

    def get_detail(id, metaTemplateId):
        clock.started("detail")
        clock.now += detail_seconds
        detail = details.get(metaTemplateId.removeprefix("meta-"), {})
        if isinstance(detail, Exception):
            raise detail
        return {"template": json.dumps(detail)}

    eum.get_linked_whatsapp_business_account_phone_number.side_effect = phone_lookup
    eum.list_linked_whatsapp_business_accounts.side_effect = list_accounts
    eum.list_whatsapp_message_templates.side_effect = list_templates
    eum.get_whatsapp_message_template.side_effect = get_detail
    return eum


def _list(stack, clock, *, registry_seconds=0.0, status=200, **stub):
    """Call the list route for *stack*, its registry read taking *registry_seconds*.

    The response must carry *status*.
    """
    from routes import templates

    def get_registry(_stack_id):
        clock.now += registry_seconds
        return stack

    with (
        patch.object(templates, "_get_registry", side_effect=get_registry),
        patch.object(templates, "boto3") as b3,
        patch.object(templates, "time", clock),
    ):
        eum = _eum(b3, clock, **stub)
        resp = templates.handle_get_templates({"id": "stack"}, {})
    assert resp["statusCode"] == status
    return json.loads(resp["body"]), eum


def _health(registries, clock, *, registry_seconds=0.0, **stub):
    """Call the health route over *registries*, their read taking *registry_seconds*."""
    from routes import templates

    def get_all_registries():
        clock.now += registry_seconds
        return registries

    with (
        patch.object(templates, "_get_all_registries", side_effect=get_all_registries),
        patch.object(templates, "boto3") as b3,
        patch.object(templates, "time", clock),
    ):
        eum = _eum(b3, clock, **stub)
        resp = templates.handle_get_template_health({}, {})
    assert resp["statusCode"] == 200
    return json.loads(resp["body"]), eum


def _row(template):
    return (
        template["name"],
        template["checked"],
        template["category"],
        template["previousCategory"],
        template["reclassified"],
    )


BOT = _stack("bot")
BOT_WABA = {"phone-bot": "waba-bot"}


@pytest.mark.unit
def test_within_the_budget_every_template_is_checked():
    """The common case is unchanged: every detail is read and decides."""
    names = ["dev_bot_one", "dev_bot_two", "dev_bot_three"]
    stub = {
        "phones": BOT_WABA,
        "pages": {"waba-bot": [names]},
        "details": {"dev_bot_two": MOVED},
        "detail_seconds": 1.0,
    }
    listed, eum = _list(BOT, _Clock(), **stub)
    health, _ = _health({"bot-dev": BOT}, _Clock(), **stub)

    assert [_row(t) for t in listed] == [
        ("dev_bot_one", True, "UTILITY", None, False),
        ("dev_bot_two", True, "MARKETING", "UTILITY", True),
        ("dev_bot_three", True, "UTILITY", None, False),
    ]
    assert eum.get_whatsapp_message_template.call_count == 3
    assert health == {
        "reclassified": [
            {"name": "dev_bot_two", "stack": "bot-dev", "category": "MARKETING", "previousCategory": "UTILITY"}
        ],
        "nonApproved": [],
        "total": 3,
        "unchecked": [],
        "failedStacks": [],
    }


@pytest.mark.unit
def test_list_stops_reading_details_once_the_budget_is_spent():
    """Past the budget there is no call, and the template is not checked.

    Every detail here says Meta moved the template, so a template the route
    did not reach must not read as not reclassified: it keeps the listing's
    category, no previous category, and ``checked`` false. The listing order
    is kept.
    """
    names = [f"dev_bot_{n}" for n in ("one", "two", "three", "four", "five")]
    # 15 s of budget at 7 s a call: details at 0, 7 and 14 s, none at 21 s.
    listed, eum = _list(
        BOT,
        _Clock(),
        phones=BOT_WABA,
        pages={"waba-bot": [names]},
        details=dict.fromkeys(names, MOVED),
        detail_seconds=7.0,
    )

    assert [_row(t) for t in listed] == [
        ("dev_bot_one", True, "MARKETING", "UTILITY", True),
        ("dev_bot_two", True, "MARKETING", "UTILITY", True),
        ("dev_bot_three", True, "MARKETING", "UTILITY", True),
        ("dev_bot_four", False, "UTILITY", None, False),
        ("dev_bot_five", False, "UTILITY", None, False),
    ]
    assert eum.get_whatsapp_message_template.call_count == 3


@pytest.mark.unit
@pytest.mark.parametrize("slow", ["registry", "listing"])
def test_the_budget_starts_with_the_request(slow):
    """Time spent before the first detail, reading the registry or listing, is spent.

    The 30-second limit counts from the request, so a deadline taken at the
    first detail call would still let the request time out.
    """
    names = ["dev_bot_one", "dev_bot_two", "dev_bot_three"]
    listed, eum = _list(
        BOT,
        _Clock(),
        registry_seconds=_budget() - 0.5 if slow == "registry" else 0.0,
        page_seconds=_budget() - 0.5 if slow == "listing" else 0.0,
        phones=BOT_WABA,
        pages={"waba-bot": [names]},
        detail_seconds=1.0,
    )

    assert [t["checked"] for t in listed] == [True, False, False]
    assert eum.get_whatsapp_message_template.call_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("slow", ["registry", "listing"])
def test_the_health_budget_starts_with_the_request(slow):
    """Health's budget also counts from the request, not from its first detail."""
    names = ["dev_bot_one", "dev_bot_two", "dev_bot_three"]
    health, eum = _health(
        {"bot-dev": BOT},
        _Clock(),
        registry_seconds=_budget() - 0.5 if slow == "registry" else 0.0,
        page_seconds=_budget() - 0.5 if slow == "listing" else 0.0,
        phones=BOT_WABA,
        pages={"waba-bot": [names]},
        detail_seconds=1.0,
    )

    assert health["total"] == 3
    assert health["unchecked"] == [
        {"name": "dev_bot_two", "stack": "bot-dev"},
        {"name": "dev_bot_three", "stack": "bot-dev"},
    ]
    assert eum.get_whatsapp_message_template.call_count == 1


@pytest.mark.unit
def test_a_detail_that_fails_is_not_checked():
    """A failed detail call is reported as not checked, in place, and logged.

    The listing still decides the category; the next template is checked as
    usual. Health counts the template and names it as unchecked.
    """
    from routes import templates

    names = ["dev_bot_one", "dev_bot_two", "dev_bot_three"]
    stub = {
        "phones": BOT_WABA,
        "pages": {"waba-bot": [names]},
        "details": {"dev_bot_two": RuntimeError("ThrottlingException")},
        "statuses": {"dev_bot_two": "PENDING"},
    }
    with patch.object(templates, "logger") as logger:
        listed, _ = _list(BOT, _Clock(), **stub)
    logger.exception.assert_called_once_with(
        "template_detail_failed", extra={"template": "dev_bot_two"}, stacklevel=3
    )
    health, _ = _health({"bot-dev": BOT}, _Clock(), **stub)

    assert [_row(t) for t in listed] == [
        ("dev_bot_one", True, "UTILITY", None, False),
        ("dev_bot_two", False, "UTILITY", None, False),
        ("dev_bot_three", True, "UTILITY", None, False),
    ]
    assert health["total"] == 3
    assert health["nonApproved"] == [{"name": "dev_bot_two", "stack": "bot-dev", "approvalStatus": "pending"}]
    assert health["unchecked"] == [{"name": "dev_bot_two", "stack": "bot-dev"}]
    assert health["failedStacks"] == []


def _throttled():
    """What a client raises once both attempts are throttled."""
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetWhatsAppMessageTemplate"
    )


def _timed_out():
    """What a client raises once both attempts outwait the read timeout."""
    return ReadTimeoutError(endpoint_url="https://social-messaging.us-east-1.amazonaws.com/")


# Each failure, and how it is logged: (level, error_type, error_code). A
# botocore error is a warning that names its class and any error code, with no
# traceback; anything else, such as a detail that is not JSON (the ValueError)
# or a fault in the route, is logged with its traceback.
LOGGED_FAILURES = [
    pytest.param(_throttled, (logging.WARNING, "ClientError", "ThrottlingException"), id="throttled"),
    pytest.param(_timed_out, (logging.WARNING, "ReadTimeoutError", None), id="read-timeout"),
    pytest.param(lambda: ValueError("Expecting value"), (logging.ERROR, None, None), id="value-error"),
]


def _assert_logged_once(caplog, event, site, failure, expected, **context):
    """*event* was logged once, by *site*, for *failure*, as *expected*, with *context*.

    *site* is the function that handled the failure. Powertools' ``location``
    field is the record's ``funcName``, so a record that named the logging
    helper instead would not say which call failed.
    """
    level, error_type, error_code = expected
    [record] = [r for r in caplog.records if r.getMessage() == event]
    assert record.funcName == site
    assert record.levelno == level
    assert {key: getattr(record, key, None) for key in context} == context
    assert getattr(record, "error_type", None) == error_type
    assert getattr(record, "error_code", None) == error_code
    if level == logging.WARNING:
        assert record.exc_info is None
    else:
        assert record.exc_info is not None and record.exc_info[1] is failure


@pytest.mark.unit
@pytest.mark.parametrize("make_failure,expected", LOGGED_FAILURES)
def test_a_failed_detail_logs_a_traceback_only_when_not_botocore(caplog, make_failure, expected):
    """A detail call botocore fails is a warning that names the error and the template.

    Throttled, a stack with a few hundred templates fails detail after
    detail until the budget runs out, and a traceback apiece buried a real
    error; one that is not botocore's still carries its traceback. The
    templates on either side are read, and log nothing.
    """
    failure = make_failure()
    caplog.set_level(logging.WARNING)
    listed, _ = _list(
        BOT,
        _Clock(),
        phones=BOT_WABA,
        pages={"waba-bot": [["dev_bot_one", "dev_bot_two", "dev_bot_three"]]},
        details={"dev_bot_two": failure},
    )

    assert [t["checked"] for t in listed] == [True, False, True]
    _assert_logged_once(
        caplog, "template_detail_failed", "_read_template_detail", failure, expected, template="dev_bot_two"
    )
    assert [r.getMessage() for r in caplog.records] == ["template_detail_failed"]


@pytest.mark.unit
@pytest.mark.parametrize("make_failure,expected", LOGGED_FAILURES)
@pytest.mark.parametrize("where", ["phone-lookup", "listing-page"])
def test_a_failed_stack_logs_a_traceback_only_when_not_botocore(caplog, where, make_failure, expected):
    """Health logs a stack it could not read the way the detail calls are logged, naming the stack."""
    failure = make_failure()
    phone, pages = {
        "phone-lookup": (failure, {}),
        "listing-page": ("waba-broken", {"waba-broken": [failure]}),
    }[where]
    caplog.set_level(logging.WARNING)
    health, _ = _health(
        {"broken-dev": _stack("broken"), "bot-dev": BOT},
        _Clock(),
        phones={"phone-broken": phone, **BOT_WABA},
        pages={**pages, "waba-bot": [["dev_bot_one"]]},
    )

    assert (health["failedStacks"], health["total"], health["unchecked"]) == (["broken-dev"], 1, [])
    _assert_logged_once(
        caplog, "template_stack_failed", "handle_get_template_health", failure, expected, stack="broken-dev"
    )
    assert [r.getMessage() for r in caplog.records] == ["template_stack_failed"]


@pytest.mark.unit
def test_health_spends_one_budget_across_stacks_in_registry_order():
    """Health has one budget for the whole request, not one per stack.

    The first stack spends most of it, so the second gets one detail and its
    other templates are unchecked, in listing order, and absent from
    ``reclassified`` although Meta moved them.
    """
    alpha = ["dev_alpha_one", "dev_alpha_two"]
    beta = ["dev_beta_one", "dev_beta_two", "dev_beta_three"]
    # 15 s of budget at 7 s a call: details at 0, 7 and 14 s, none at 21 s.
    health, eum = _health(
        {"alpha-dev": _stack("alpha"), "beta-dev": _stack("beta")},
        _Clock(),
        phones={"phone-alpha": "waba-alpha", "phone-beta": "waba-beta"},
        pages={"waba-alpha": [alpha], "waba-beta": [beta]},
        details=dict.fromkeys(alpha + beta, MOVED),
        detail_seconds=7.0,
    )

    assert [(r["name"], r["stack"]) for r in health["reclassified"]] == [
        ("dev_alpha_one", "alpha-dev"),
        ("dev_alpha_two", "alpha-dev"),
        ("dev_beta_one", "beta-dev"),
    ]
    assert health["unchecked"] == [
        {"name": "dev_beta_two", "stack": "beta-dev"},
        {"name": "dev_beta_three", "stack": "beta-dev"},
    ]
    assert health["total"] == 5
    assert health["failedStacks"] == []
    assert eum.get_whatsapp_message_template.call_count == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    "phone,pages",
    [
        pytest.param(RuntimeError("AccessDeniedException"), {}, id="phone-lookup-raises"),
        pytest.param("", {}, id="phone-has-no-waba"),
        pytest.param("waba-broken", {"waba-broken": [RuntimeError("ThrottlingException")]}, id="first-page-raises"),
    ],
)
def test_health_names_a_stack_it_could_not_read(phone, pages):
    """A stack health could not read is failed, and the next stack still counts."""
    health, _ = _health(
        {"broken-dev": _stack("broken"), "bot-dev": BOT},
        _Clock(),
        phones={"phone-broken": phone, **BOT_WABA},
        pages={**pages, "waba-bot": [["dev_bot_one"]]},
    )

    assert health == {
        "reclassified": [],
        "nonApproved": [],
        "total": 1,
        "unchecked": [],
        "failedStacks": ["broken-dev"],
    }


@pytest.mark.unit
def test_health_keeps_what_a_stack_listed_before_a_later_page_failed():
    """The first page still counts, and the stack is failed: its listing is incomplete."""
    health, _ = _health(
        {"bot-dev": BOT},
        _Clock(),
        phones=BOT_WABA,
        pages={"waba-bot": [["dev_bot_one", "dev_bot_two"], RuntimeError("ThrottlingException")]},
        details={"dev_bot_two": MOVED},
        statuses={"dev_bot_one": "REJECTED"},
    )

    assert health == {
        "reclassified": [
            {"name": "dev_bot_two", "stack": "bot-dev", "category": "MARKETING", "previousCategory": "UTILITY"}
        ],
        "nonApproved": [{"name": "dev_bot_one", "stack": "bot-dev", "approvalStatus": "rejected"}],
        "total": 2,
        "unchecked": [],
        "failedStacks": ["bot-dev"],
    }


@pytest.mark.unit
def test_health_skips_a_stack_without_a_phone_number():
    """No phone number means nothing to read, which is not a failure."""
    health, eum = _health(
        {"nophone-dev": _stack("nophone", phone=False), "bot-dev": BOT},
        _Clock(),
        phones=BOT_WABA,
        pages={"waba-bot": [["dev_bot_one"]]},
    )

    assert health["failedStacks"] == []
    assert health["total"] == 1
    assert [c.kwargs["id"] for c in eum.get_linked_whatsapp_business_account_phone_number.call_args_list] == [
        "phone-bot"
    ]


@pytest.mark.unit
def test_health_names_failed_stacks_in_registry_order():
    """Failures of every kind, between healthy and skipped stacks, keep the registry's order."""
    registries = {
        "lookup-dev": _stack("lookup"),
        "ok-dev": _stack("ok"),
        "nowaba-dev": _stack("nowaba"),
        "nophone-dev": _stack("nophone", phone=False),
        "later-dev": _stack("later"),
        "first-dev": _stack("first"),
    }
    health, _ = _health(
        registries,
        _Clock(),
        phones={
            "phone-lookup": RuntimeError("AccessDeniedException"),
            "phone-ok": "waba-ok",
            "phone-nowaba": "",
            "phone-later": "waba-later",
            "phone-first": "waba-first",
        },
        pages={
            "waba-ok": [["dev_ok_one"]],
            "waba-later": [["dev_later_one"], RuntimeError("ThrottlingException")],
            "waba-first": [RuntimeError("ThrottlingException")],
        },
    )

    assert health["failedStacks"] == ["lookup-dev", "nowaba-dev", "later-dev", "first-dev"]
    assert health["total"] == 2
    assert health["unchecked"] == []


@pytest.mark.unit
@pytest.mark.parametrize("route", ["list", "detail", "health"])
def test_every_eum_client_has_the_timeouts_and_one_retry(route):
    """Each socialmessaging client a template route creates carries ``_EUM_CONFIG``.

    Without it botocore waits 60 s to connect and 60 s to read, and tries up
    to 5 times, so one call started before the deadline could still carry
    the request past 30 s. Health creates one client per Region.
    """
    from routes import templates

    east = BOT
    west = {**_stack("west"), "region": "us-west-2"}
    with (
        patch.object(templates, "_get_registry", return_value=east),
        patch.object(templates, "_get_all_registries", return_value={"bot-dev": east, "west-dev": west}),
        patch.object(templates, "boto3") as b3,
    ):
        _eum(
            b3,
            _Clock(),
            phones={**BOT_WABA, "phone-west": "waba-west"},
            pages={"waba-bot": [["dev_bot_one"]], "waba-west": [["dev_west_one"]]},
        )
        if route == "list":
            resp = templates.handle_get_templates({"id": "bot-dev"}, {})
        elif route == "detail":
            resp = templates.handle_get_template_detail({"id": "bot-dev", "template_name": "dev_bot_one"}, {})
        else:
            resp = templates.handle_get_template_health({}, {})

    assert resp["statusCode"] == 200
    regions = ["us-east-1", "us-west-2"] if route == "health" else ["us-east-1"]
    assert b3.client.call_args_list == [
        call("socialmessaging", region_name=region, config=templates._EUM_CONFIG) for region in regions
    ]
    config = templates._EUM_CONFIG
    assert (config.connect_timeout, config.read_timeout, config.retries) == (
        2,
        3,
        {"mode": "standard", "total_max_attempts": 2},
    )


class _Raw:
    """The raw body of a canned botocore response."""

    def __init__(self, body):
        self._body = body

    def stream(self, **_):
        yield self._body


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["throttled", "read-timeout"])
def test_a_throttled_or_slow_call_is_tried_twice_then_fails(failure):
    """A real botocore client with ``_EUM_CONFIG`` makes 2 attempts and backs off at most 1 s.

    In a client Config ``max_attempts`` counts the retries, so
    ``max_attempts: 2`` would make 3 attempts and 18 s. Nothing leaves the
    host: a before-send hook answers every attempt, and the backoff is
    recorded instead of slept.
    """
    import boto3
    import botocore.endpoint
    from botocore.awsrequest import AWSResponse
    from routes import templates

    attempts = []

    def answer(request, **_):
        attempts.append(request.url)
        if failure == "read-timeout":
            raise ReadTimeoutError(endpoint_url=request.url)
        headers = {"x-amzn-ErrorType": "ThrottlingException"}
        return AWSResponse(request.url, 429, headers, _Raw(b'{"message": "Rate exceeded"}'))

    client = boto3.client("socialmessaging", region_name="us-east-1", config=templates._EUM_CONFIG)
    client.meta.events.register("before-send", answer)
    with (
        patch.object(botocore.endpoint.time, "sleep") as sleep,
        pytest.raises(ClientError if failure == "throttled" else ReadTimeoutError),
    ):
        client.get_linked_whatsapp_business_account_phone_number(id="phone-bot")

    assert len(attempts) == 2
    [backoff] = [c.args[0] for c in sleep.call_args_list]
    assert 0 <= backoff <= 1


def _lambda_timeout(tf: str, label: str) -> int:
    """The ``timeout`` argument of ``resource "aws_lambda_function" "<label>"`` in *tf*.

    Only the resource's own arguments count. Comments, strings (their
    interpolations hold braces) and whatever is nested in it, a block, a map
    or a list, are dropped first, so a comment or a nested key named
    ``timeout`` cannot pass for it; and a timeout that is missing (Lambda's
    default is 3 s), set twice or not a number fails here rather than be
    guessed at.
    """
    headers = list(re.finditer(rf'^resource\s+"aws_lambda_function"\s+"{label}"\s*\{{', tf, re.MULTILINE))
    assert len(headers) == 1, f"{len(headers)} aws_lambda_function.{label} resources"
    own, depth, i = [], 1, headers[0].end()
    while depth:
        assert i < len(tf), f"aws_lambda_function.{label} is never closed"
        if tf[i] == '"':
            i += 1
            while tf[i] != '"':
                i += 2 if tf[i] == "\\" else 1
            own.append('""' if depth == 1 else "")
        elif tf[i] == "#" or tf.startswith("//", i):
            i = tf.index("\n", i) - 1
        elif tf.startswith("/*", i):
            end = tf.index("*/", i) + 1
            own.append("\n" * tf.count("\n", i, end))
            i = end
        elif tf[i] in "{[(":
            depth += 1
        elif tf[i] in "}])":
            depth -= 1
        elif depth == 1 or tf[i] == "\n":
            own.append(tf[i])
        i += 1
    values = re.findall(r"^\s*timeout\s*=\s*(.*?)\s*$", "".join(own), re.MULTILINE)
    assert len(values) == 1 and values[0].isdigit(), f"aws_lambda_function.{label} timeout: {values}"
    return int(values[0])


@pytest.mark.unit
def test_the_backend_timeout_is_the_functions_own_argument():
    """``_lambda_timeout`` reads the named function's ``timeout`` and nothing that looks like it."""
    tf = """
resource "aws_lambda_function" "other" {
  timeout = 900
}

resource "aws_lambda_function" "backend" {
  function_name = "${local.prefix}-api {"
  # a lone " or { in a comment
  // timeout = 900
  /*
timeout = 900
  */
  environment {
    variables = {
      timeout = "900"
    }
  }
  layers  = [{ timeout = 900 }]
  timeout = 25 # seconds
}

resource "aws_lambda_function" "after" {
  timeout = 900
}
"""
    assert _lambda_timeout(tf, "backend") == 25
    for broken in ("timeout = 25\n  timeout = 26", "timeout = var.timeout", "memory_size = 256"):
        with pytest.raises(AssertionError):
            _lambda_timeout(f'resource "aws_lambda_function" "backend" {{\n  {broken}\n}}\n', "backend")


@pytest.mark.unit
def test_the_deadline_leaves_room_for_one_call():
    """The deadline, the slowest single call and the rest of the request fit in the backend's time.

    That time is the backend Lambda's timeout, read from the console's
    Terraform, and at most the HTTP API's 30 s, so lowering the timeout or
    raising the budget fails here: the two are separate literals. The
    slowest call waits out both timeouts on every attempt, and before each
    retry the standard mode's backoff, at most min(2 ** (retry - 1), 20) s
    (ExponentialBackoff in botocore/retries/standard.py). The docs and the
    route's comment state the 11 s.
    """
    from routes import templates

    config = templates._EUM_CONFIG
    attempts = config.retries["total_max_attempts"]
    backoff = sum(min(2 ** (retry - 1), 20) for retry in range(1, attempts))
    slowest_call = attempts * (config.connect_timeout + config.read_timeout) + backoff
    limit = min(_lambda_timeout(API_MODULE.read_text(), "backend"), HTTP_API_LIMIT_SECONDS)

    assert slowest_call == 11
    assert templates._EUM_BUDGET_SECONDS + slowest_call + OUTSIDE_EUM_SECONDS <= limit


@pytest.mark.unit
@pytest.mark.parametrize(
    "stub,started",
    [
        pytest.param({"registry_seconds": "budget"}, [], id="phone-lookup"),
        pytest.param(
            {"phones": {"phone-bot": RuntimeError("AccessDeniedException")}, "phone_seconds": "budget"},
            ["phone"],
            id="waba-fallback",
        ),
        pytest.param(
            {"phones": {"phone-bot": ""}, "accounts": [[], ["waba-bot"]], "account_seconds": "budget"},
            ["phone", "accounts"],
            id="waba-fallback-next-page",
        ),
        pytest.param(
            {"pages": {"waba-bot": [["dev_bot_one"], ["dev_bot_two"]]}, "page_seconds": "budget"},
            ["phone", "page"],
            id="listing-next-page",
        ),
    ],
)
def test_list_fails_rather_than_start_a_call_past_the_deadline(stub, started):
    """Out of time before a lookup or a listing page, the list route answers 500.

    A list cut short would pass for the stack's whole list, so the request
    fails and the console names the stack as not loaded. No call of any kind
    starts at or past the deadline, and a template on a page already read
    gets no detail call either.
    """
    stub = {key: _budget() if value == "budget" else value for key, value in stub.items()}
    stub = {"phones": BOT_WABA, "pages": {"waba-bot": [["dev_bot_one"]]}, **stub}
    clock = _Clock()
    body, _ = _list(BOT, clock, status=500, **stub)

    assert body["error"].startswith("Templates error")
    assert clock.operations() == started
    clock.assert_no_call_after(1000.0 + _budget())


def _full_pages(prefix, count):
    """*count* full listing pages of template names starting with *prefix*."""
    from routes import templates

    return [[f"{prefix}{page}_{n:03d}" for n in range(templates._PAGE_SIZE)] for page in range(count)]


@pytest.mark.unit
def test_list_reads_every_page_before_the_details():
    """The details wait for the whole listing, so they cannot use up the time its later pages need.

    Read page by page, a first page's 100 details at the quota's pace spent
    the budget before the second page could start, and the request failed
    for any stack with more than one page of templates. Listed first, all
    300 come back, the ones the budget did not reach marked not checked.
    """
    pages = _full_pages("dev_bot_", 3)
    clock = _Clock()
    # 15 s of budget at 0.13 s a detail: 116 details, the last at 14.95 s.
    listed, _ = _list(BOT, clock, phones=BOT_WABA, pages={"waba-bot": pages}, detail_seconds=0.13)

    assert [t["name"] for t in listed] == [name for page in pages for name in page]
    assert [t["checked"] for t in listed] == [True] * 116 + [False] * 184
    assert clock.operations() == ["phone", "page", "page", "page"] + ["detail"] * 116
    clock.assert_no_call_after(1000.0 + _budget())


@pytest.mark.unit
def test_health_lists_every_stack_before_reading_details():
    """Details wait for every stack's listing, so they cannot fail the stacks listed after them.

    alpha has two full pages and beta, after it, a few templates. Read stack
    by stack, alpha's first-page details spent the budget, so alpha's second
    page and beta's lookup never started and both were failed, beta's
    templates missing from the counts. Listed first, both are counted in
    full, and the details the budget did not reach, in registry order, are
    unchecked.
    """
    alpha = [name for page in _full_pages("dev_alpha_", 2) for name in page]
    beta = ["dev_beta_one", "dev_beta_two", "dev_beta_three", "dev_beta_four", "dev_beta_five"]
    clock = _Clock()
    # 15 s of budget at 0.13 s a detail: 116 details, the last at 14.95 s.
    health, _ = _health(
        {"alpha-dev": _stack("alpha"), "beta-dev": _stack("beta")},
        clock,
        phones={"phone-alpha": "waba-alpha", "phone-beta": "waba-beta"},
        pages={"waba-alpha": [alpha[:100], alpha[100:]], "waba-beta": [beta]},
        statuses={"dev_beta_five": "REJECTED"},
        detail_seconds=0.13,
    )

    assert health["total"] == 205
    assert health["failedStacks"] == []
    assert health["nonApproved"] == [{"name": "dev_beta_five", "stack": "beta-dev", "approvalStatus": "rejected"}]
    assert health["unchecked"] == [{"name": name, "stack": "alpha-dev"} for name in alpha[116:]] + [
        {"name": name, "stack": "beta-dev"} for name in beta
    ]
    assert clock.operations() == ["phone", "page", "page", "phone", "page"] + ["detail"] * 116
    clock.assert_no_call_after(1000.0 + _budget())


@pytest.mark.unit
def test_health_leaves_later_stacks_listed_when_the_details_run_out():
    """Details that run out of time leave every stack listed and counted, the rest unchecked.

    15 s of budget at 7 s a detail: alpha's at 0 s and beta's at 7 and 14 s.
    beta's other templates, on its second page too, and gamma's are counted
    and unchecked; no stack is failed.
    """
    alpha = ["dev_alpha_one"]
    beta = ["dev_beta_one", "dev_beta_two", "dev_beta_three"]
    clock = _Clock()
    health, _ = _health(
        {
            "alpha-dev": _stack("alpha"),
            "beta-dev": _stack("beta"),
            "nophone-dev": _stack("nophone", phone=False),
            "gamma-dev": _stack("gamma"),
        },
        clock,
        phones={"phone-alpha": "waba-alpha", "phone-beta": "waba-beta", "phone-gamma": "waba-gamma"},
        pages={
            "waba-alpha": [alpha],
            "waba-beta": [beta, ["dev_beta_four"]],
            "waba-gamma": [["dev_gamma_one"]],
        },
        details=dict.fromkeys(alpha + beta, MOVED),
        statuses={"dev_beta_three": "REJECTED"},
        detail_seconds=7.0,
    )

    moved = {"category": "MARKETING", "previousCategory": "UTILITY"}
    assert health == {
        "reclassified": [
            {"name": "dev_alpha_one", "stack": "alpha-dev", **moved},
            {"name": "dev_beta_one", "stack": "beta-dev", **moved},
            {"name": "dev_beta_two", "stack": "beta-dev", **moved},
        ],
        "nonApproved": [{"name": "dev_beta_three", "stack": "beta-dev", "approvalStatus": "rejected"}],
        "total": 6,
        "unchecked": [
            {"name": "dev_beta_three", "stack": "beta-dev"},
            {"name": "dev_beta_four", "stack": "beta-dev"},
            {"name": "dev_gamma_one", "stack": "gamma-dev"},
        ],
        "failedStacks": [],
    }
    assert clock.operations() == ["phone", "page", "phone", "page", "page", "phone", "page"] + ["detail"] * 3
    clock.assert_no_call_after(1000.0 + _budget())


@pytest.mark.unit
def test_health_fails_the_stack_the_deadline_stops_and_every_stack_after_it():
    """Health starts no call past the deadline; the stack it stops keeps its first page.

    The listing is slow, 8 s a page. alpha is listed in full. beta's first
    page ends at 16 s, so its second page never starts and beta is failed,
    its first page still counted. gamma, after it, gets no phone lookup and
    is failed too; a stack without a phone number is still skipped, not
    failed. No detail is read, so every counted template is unchecked,
    although Meta moved each one.
    """
    alpha = ["dev_alpha_one"]
    beta = ["dev_beta_one", "dev_beta_two"]
    clock = _Clock()
    health, _ = _health(
        {
            "alpha-dev": _stack("alpha"),
            "beta-dev": _stack("beta"),
            "nophone-dev": _stack("nophone", phone=False),
            "gamma-dev": _stack("gamma"),
        },
        clock,
        phones={"phone-alpha": "waba-alpha", "phone-beta": "waba-beta", "phone-gamma": "waba-gamma"},
        pages={
            "waba-alpha": [alpha],
            "waba-beta": [beta, ["dev_beta_three"]],
            "waba-gamma": [["dev_gamma_one"]],
        },
        details=dict.fromkeys(alpha + beta + ["dev_beta_three", "dev_gamma_one"], MOVED),
        statuses={"dev_beta_two": "REJECTED"},
        page_seconds=8.0,
    )

    assert health == {
        "reclassified": [],
        "nonApproved": [{"name": "dev_beta_two", "stack": "beta-dev", "approvalStatus": "rejected"}],
        "total": 3,
        "unchecked": [
            {"name": "dev_alpha_one", "stack": "alpha-dev"},
            {"name": "dev_beta_one", "stack": "beta-dev"},
            {"name": "dev_beta_two", "stack": "beta-dev"},
        ],
        "failedStacks": ["beta-dev", "gamma-dev"],
    }
    assert clock.operations() == ["phone", "page", "phone", "page"]
    clock.assert_no_call_after(1000.0 + _budget())


@pytest.mark.unit
@pytest.mark.parametrize("slow", ["registry", "phone"])
def test_health_fails_every_stack_once_the_deadline_has_passed(slow):
    """Past the deadline before a stack's lookup or first page, it and the rest are failed.

    With the registry read taking the whole budget no call starts; with the
    first phone lookup taking it, that stack's listing does not start, and
    no other stack is looked up.
    """
    clock = _Clock()
    health, _ = _health(
        {
            "alpha-dev": _stack("alpha"),
            "nophone-dev": _stack("nophone", phone=False),
            "beta-dev": _stack("beta"),
        },
        clock,
        registry_seconds=_budget() if slow == "registry" else 0.0,
        phone_seconds=_budget() if slow == "phone" else 0.0,
        phones={"phone-alpha": "waba-alpha", "phone-beta": "waba-beta"},
        pages={"waba-alpha": [["dev_alpha_one"]], "waba-beta": [["dev_beta_one"]]},
    )

    assert health == {
        "reclassified": [],
        "nonApproved": [],
        "total": 0,
        "unchecked": [],
        "failedStacks": ["alpha-dev", "beta-dev"],
    }
    assert clock.operations() == ([] if slow == "registry" else ["phone"])
    clock.assert_no_call_after(1000.0 + _budget())
