# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""GET /api/schedules says which time zone each schedule is read in.

A schedule can name an IANA zone (EventBridge's ScheduleExpressionTimezone),
so a daily cron(0 9 * * ? *) may fire at 9am Mexico City time. Without the
zone the console showed a bare cron that an operator reads as UTC.
"""

import json
from unittest.mock import MagicMock

import pytest

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.


@pytest.fixture
def scheduler(monkeypatch):
    from routes import schedules

    details = {
        "zoned": {
            "Name": "zoned",
            "State": "ENABLED",
            "ScheduleExpression": "cron(0 9 * * ? *)",
            "ScheduleExpressionTimezone": "America/Mexico_City",
            "Target": {"Arn": "arn:aws:lambda:us-east-1:1:function:f", "Input": "{}"},
        },
        "plain": {
            "Name": "plain",
            "State": "ENABLED",
            "ScheduleExpression": "at(2026-10-10T14:00:00)",
            "Target": {"Arn": "arn:aws:lambda:us-east-1:1:function:f", "Input": "{}"},
        },
    }
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {"Schedules": [{"Name": name} for name in details]}
    ]
    client.get_schedule.side_effect = lambda Name: details[Name]
    monkeypatch.setattr(schedules.boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(schedules, "_get_all_registries", lambda: {})
    return client


def test_each_schedule_carries_its_time_zone(scheduler):
    from routes import schedules

    resp = schedules.handle_list_schedules({}, {})

    assert resp["statusCode"] == 200
    by_name = {s["name"]: s for s in json.loads(resp["body"])}
    assert by_name["zoned"]["timezone"] == "America/Mexico_City"
    # EventBridge omits the field for a schedule written in UTC.
    assert by_name["plain"]["timezone"] == "UTC"
