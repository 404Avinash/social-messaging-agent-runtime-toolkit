# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A CloudWatch Logs stand-in for the tests that read the newest log lines."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock


class FakeLogs:
    """filter_log_events over fixed event lists: oldest first, ``limit`` per page.

    Like the real API, ``startTime`` and ``endTime`` are both inclusive, and a
    filter pattern matches an event holding every term, or any one of them
    when each is ``?``-prefixed (``?ERROR ?Traceback``).

    ``scan`` models how the real API pages a large log group: each call reads
    that many lines of the range, matching or not, and returns the matches
    among them with a ``nextToken`` while lines are left, so a sparse filter
    gets partial or empty pages that do not mean the end. ``newest_first``
    models a botocore that knows ``startFromHead``: with it False, the range is
    read from its newest line back. Without it the client looks like an older
    botocore, which has no such parameter.
    """

    def __init__(self, groups: dict[str, list[dict]], scan: int | None = None, newest_first: bool = False):
        self.groups = groups
        self.scan = scan
        self.filter_log_events = MagicMock(side_effect=self._filter)
        if newest_first:
            self.meta = MagicMock()
            self.meta.service_model.operation_model.return_value.input_shape.members = {"startFromHead": None}

    def _filter(self, logGroupName, startTime, endTime, filterPattern="", limit=10000, nextToken=None, startFromHead=True, **_):
        terms = [t for t in filterPattern.split() if t]
        either = bool(terms) and all(t.startswith("?") for t in terms)

        def matches(event: dict) -> bool:
            if either:
                return any(t[1:] in event["message"] for t in terms)
            return all(t in event["message"] for t in terms)

        lines = [
            e
            for e in sorted(self.groups.get(logGroupName, []), key=lambda e: e["timestamp"])
            if startTime <= e["timestamp"] <= endTime
        ]
        if not startFromHead:
            lines.reverse()
        offset = int(nextToken or 0)
        if self.scan is None:
            matching = [e for e in lines if matches(e)]
            page = matching[offset : offset + limit]
            resp = {"events": page}
            if offset + limit < len(matching):
                resp["nextToken"] = str(offset + limit)
            return resp
        page = []
        end = offset
        while end < len(lines) and end < offset + self.scan and len(page) < limit:
            if matches(lines[end]):
                page.append(lines[end])
            end += 1
        resp = {"events": page}
        if end < len(lines):
            resp["nextToken"] = str(end)
        return resp


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def spread(count: int, newest_minutes_ago: float, oldest_minutes_ago: float) -> list[int]:
    """``count`` timestamps, oldest first, spread evenly between the two ages."""
    now = datetime.now(UTC)
    span = oldest_minutes_ago - newest_minutes_ago
    return [
        ms(now - timedelta(minutes=oldest_minutes_ago - span * i / max(count - 1, 1)))
        for i in range(count)
    ]


def newest(events: list[dict], count: int, include=lambda e: True) -> list[str]:
    """The messages of the newest ``count`` events ``include`` keeps, newest first."""
    kept = [e for e in sorted(events, key=lambda e: e["timestamp"]) if include(e)]
    return [e["message"] for e in reversed(kept[-count:])]
