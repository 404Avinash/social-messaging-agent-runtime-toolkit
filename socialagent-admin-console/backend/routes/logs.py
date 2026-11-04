# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Logs routes: search CloudWatch logs across a stack's Lambdas."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from aws_lambda_powertools import Logger

from routes.stacks import (
    UNREAD_BUDGET, LogRead, _CallBudget, _call_shares, _lambda_suffixes, _log_read_response, _newest_log_events,
)
from shared.auth import require_group
from shared.clients import client
from shared.config import DEFAULT_REGION
from shared.http import error_response, json_response
from shared.registry import get_all_registries, get_registry
from shared.resolver import app, current_body, from_legacy

logger = Logger(service="admin-console-backend", child=True)

_ROWS_PER_GROUP = 50

# One search starts at most this many filter_log_events calls in all, taken
# from one budget its log groups share, and starts none after this many
# seconds. The FilterLogEvents quota is account-wide (10 calls a second in
# most Regions), and the backend times out at 30 s. The calls are split as
# evenly as they go, the first groups in registry order taking any left over.
# Each group needs at least one call, so a search over more log groups than
# this reads the first ones in registry order, lists the rest as unreadable
# for the budget, and logs how many it left out.
# The cap counts SDK calls: with the client's five retries, one call can be
# up to six requests.
_LOG_SEARCH_CALLS = 48
_LOG_SEARCH_SECONDS = 20
# The widest window a search reads, in minutes: the Logs page's longest, and
# what the call budget above was sized for.
_MAX_MINUTES = 1440

# The Lambda platform's per-invocation lines. A filter pattern cannot drop them
# without also dropping application lines that use the same words, so they are
# dropped as read, before they take a log group's rows.
_PLATFORM_PREFIXES = ("START ", "END ", "REPORT ")


def _is_application_line(event: dict) -> bool:
    msg = event.get("message", "").strip()
    return bool(msg) and not msg.startswith(_PLATFORM_PREFIXES)


def search_logs_impl(body: dict) -> dict:
    """Search CloudWatch logs across stack Lambdas.

    Answers ``{results, unreadable}``, where ``unreadable`` lists each
    Lambda whose log group was not read in full and why (see
    ``_log_read_response``), or 400 when CloudWatch Logs refuses the
    caller's filter pattern.
    """
    stack_id = body.get("stack", "") if body else ""
    search_text = body.get("q", "") if body else ""
    # Coerce defensively, like _parse_page_size: an unguarded int() on a
    # client-supplied value ({"minutes": "abc"} / null / [1]) would throw out of
    # the handler and fail the invocation with no JSON body.
    try:
        minutes = int(body.get("minutes", 60)) if body else 60
    except (TypeError, ValueError):
        minutes = 60
    # A window that ends before it starts, or starts before 1970, is an
    # invalid parameter to CloudWatch Logs, which this route reports as an
    # invalid filter pattern.
    minutes = 60 if minutes < 1 else min(minutes, _MAX_MINUTES)
    try:
        registries = get_all_registries() if not stack_id else {stack_id: get_registry(stack_id)}
        # (log group, the client for its stack's region)
        log_groups = []
        for sid, cfg in registries.items():
            if not cfg:
                continue
            webhook = cfg.get("outputs", {}).get("webhook_lambda", "")
            prefix = webhook.rsplit("-webhook", 1)[0] if webhook else sid
            reader = client("logs", cfg.get("region") or DEFAULT_REGION)
            for suffix in _lambda_suffixes(cfg):
                log_groups.append((f"/aws/lambda/{prefix}-{suffix}", reader))
        # Each group needs a call, so the groups past the budget's count are
        # left out here, not by whichever workers ask last: a search over the
        # same stacks then reads the same groups every time.
        to_read = log_groups[:_LOG_SEARCH_CALLS]
        not_read = len(log_groups) - len(to_read)
        shares = _call_shares(_LOG_SEARCH_CALLS, len(to_read))
        budget = _CallBudget(_LOG_SEARCH_CALLS, deadline=time.monotonic() + _LOG_SEARCH_SECONDS)

        def group_rows(group: tuple, calls: int) -> tuple[LogRead, bool]:
            """The group's rows and why it stopped short, if it did, and
            whether the budget let it make a call."""
            lg, reader = group
            share = budget.share(calls)
            try:
                # filter_log_events returns the oldest lines first, so a single
                # page showed the start of the window, not what just happened.
                read = _newest_log_events(
                    lg, search_text or "", hours=minutes / 60, keep=_ROWS_PER_GROUP,
                    include=_is_application_line, client=reader, budget=share,
                )
                rows = [{
                    "timestamp": datetime.fromtimestamp(ev["timestamp"] / 1000, tz=UTC).isoformat(),
                    "logGroup": lg,
                    "lambda": lg.split("/")[-1],
                    "message": ev.get("message", "").strip()[:2000],
                } for ev in read.events]
                read = LogRead(rows, read.reason)
            except Exception as e:
                # _newest_log_events reports a failed call itself; this is a
                # fault in the code around it, so it keeps its traceback.
                logger.exception("log_search_group_failed", extra={"log_group": lg})
                read = LogRead([], type(e).__name__)
            return read, share.taken > 0

        # A quiet group is read once per widening window, so the groups are read
        # in parallel (boto3 clients are thread-safe), as _lambda_metrics probes.
        results, unreadable, read_any = [], [], False
        with ThreadPoolExecutor(max_workers=min(8, len(to_read) or 1)) as pool:
            for (lg, _), (read, called) in zip(to_read, pool.map(group_rows, to_read, shares)):
                results.extend(read.events)
                read_any = read_any or read.read_something()
                if read.reason:
                    unreadable.append({"lambda": lg.split("/")[-1], "reason": read.reason})
                # The deadline can pass before a group's first call.
                not_read += not called
        # The search's own limit left these out, so they are listed as the
        # budget's, not as failures.
        unreadable += [{"lambda": lg.split("/")[-1], "reason": UNREAD_BUDGET} for lg, _ in log_groups[_LOG_SEARCH_CALLS:]]
        # Every group is searched with the same pattern, so one refusal is the
        # pattern's. Its text stays out of the answer and the log, since it is
        # what the operator searched for, a phone number included.
        if search_text and any(u["reason"] == "InvalidParameterException" for u in unreadable):
            return json_response(400, {"error": "Invalid filter pattern"})
        if not_read:
            logger.warning("log_search_incomplete", extra={"log_groups": len(log_groups), "not_read": not_read})
        results.sort(key=lambda x: x["timestamp"], reverse=True)
        return _log_read_response(results[:200], unreadable, read_any)
    except Exception:
        return error_response(500, "Log search failed")


@app.put("/api/logs")
@require_group("operator")
def search_logs():
    return from_legacy(search_logs_impl(current_body()))
