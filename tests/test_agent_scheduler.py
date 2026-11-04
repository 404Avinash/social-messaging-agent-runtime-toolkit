# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``socialagent_runtime.scheduler.schedule``: schedule identity and time zones.

An id the caller does not supply is generated, and a create on an existing
name updates that schedule. So two requests that differ only in when they fire
must get different ids, or the second one silently replaces the first while
the tool reports both as scheduled.

A recurring schedule is read in UTC unless it names a time zone, so a daily
9am written in UTC moves by an hour in local time whenever the user's zone
changes its offset.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import scheduler as sched  # noqa: E402

PHONE = "+525555550100"
DIGITS = "525555550100"
_AUTO_ID = re.compile(rf"^{DIGITS}-(reminder|invoke_agent)-[0-9a-f]{{8}}$")


class _ProdConfig:
    """Deployed-mode config stub (the real one is a frozen singleton)."""

    is_dev_mode = False
    scheduler_prefix = "dev-bot"
    scheduler_lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:scheduler"
    has_scheduler_lambda = True
    scheduler_role_arn = "arn:aws:iam::123456789012:role/scheduler"
    region = "us-east-1"


@pytest.fixture
def created(monkeypatch):
    """Deployed mode with the EventBridge call recorded instead of made."""
    monkeypatch.setattr(sched, "config", _ProdConfig())
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    recorder = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", recorder)
    return recorder


@pytest.fixture
def clock(monkeypatch):
    """The scheduler's clock, moved by assigning ``clock.current``.

    A delay resolves against the current time, so a test that compares the ids
    of two delayed schedules must not run across a minute boundary.
    """

    class _Clock(datetime):
        current = datetime(2026, 10, 10, 10, 0, 5, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(sched, "datetime", _Clock)
    return _Clock


async def _ids(calls: list[dict]) -> list[str]:
    return [await sched.schedule(phone=PHONE, **kwargs) for kwargs in calls]


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param(
            [
                {
                    "operation": "reminder",
                    "data": {"message": "Tomar pastilla"},
                    "at": datetime(2026, 10, 10, 14, 0, tzinfo=UTC),
                },
                {
                    "operation": "reminder",
                    "data": {"message": "Tomar pastilla"},
                    "at": datetime(2026, 10, 10, 23, 0, tzinfo=UTC),
                },
            ],
            id="one-shot-at-two-times",
        ),
        pytest.param(
            [
                {
                    "operation": "reminder",
                    "data": {"message": "Drink water"},
                    "delay_minutes": 10,
                },
                {
                    "operation": "reminder",
                    "data": {"message": "Drink water"},
                    "delay_minutes": 120,
                },
            ],
            id="one-shot-two-delays",
        ),
        pytest.param(
            [
                {
                    "operation": "reminder",
                    "data": {"message": "Take vitamins"},
                    "cron": "0 15 ? * MON *",
                },
                {
                    "operation": "reminder",
                    "data": {"message": "Take vitamins"},
                    "cron": "0 15 ? * THU *",
                },
            ],
            id="recurring-two-crons",
        ),
        pytest.param(
            [
                {
                    "operation": "reminder",
                    "data": {"message": "Take vitamins"},
                    "cron": "0 15 ? * MON *",
                },
                {
                    "operation": "reminder",
                    "data": {"message": "Take vitamins"},
                    "delay_minutes": 2,
                },
            ],
            id="recurring-then-one-shot",
        ),
        pytest.param(
            [
                {
                    "operation": "invoke_agent",
                    "data": {"prompt": "Write a haiku"},
                    "delay_minutes": 5,
                },
                {
                    "operation": "invoke_agent",
                    "data": {"prompt": "Write a haiku"},
                    "at": datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
                },
            ],
            id="task-two-times",
        ),
    ],
)
@pytest.mark.asyncio
async def test_same_data_at_different_times_gets_different_ids(created, calls):
    ids = await _ids(calls)
    assert ids[0] != ids[1]
    names = [c.args[0] for c in created.call_args_list]
    assert names[0] != names[1]
    # The id keeps the shape ownership is read from.
    for sid, name in zip(ids, names):
        assert _AUTO_ID.fullmatch(sid), sid
        assert name == f"dev-bot-{sid.replace('_', '-')}"
        assert sched._owns_schedule(name, PHONE) is True


@pytest.mark.parametrize(
    "timing",
    [
        {"cron": "0 15 * * ? *"},
        {"cron": "cron(0 15 * * ? *)"},
        {"at": datetime(2026, 10, 10, 14, 0, tzinfo=UTC)},
        {"delay_minutes": 30},
    ],
)
@pytest.mark.asyncio
async def test_the_same_request_twice_keeps_one_id(created, clock, timing):
    """A repeated request (a retried tool call) still updates one schedule."""
    call = {"operation": "reminder", "data": {"message": "Stretch"}, **timing}
    first = await sched.schedule(phone=PHONE, **call)
    clock.current = datetime(2026, 10, 10, 10, 0, 40, tzinfo=UTC)
    second = await sched.schedule(phone=PHONE, **call)
    assert first == second


@pytest.mark.asyncio
async def test_the_same_delay_asked_again_later_is_a_second_schedule(created, clock):
    """'In 30 minutes check the oven' at 10:00 and again at 10:15 is two
    reminders, at 10:30 and 10:45. Hashing the delay as given made the second
    replace the first."""
    call = {"operation": "reminder", "data": {"message": "Revisar el horno"}}
    first = await sched.schedule(phone=PHONE, delay_minutes=30, **call)
    clock.current = datetime(2026, 10, 10, 10, 15, 5, tzinfo=UTC)
    second = await sched.schedule(phone=PHONE, delay_minutes=30, **call)
    assert first != second
    for sid in (first, second):
        assert _AUTO_ID.fullmatch(sid), sid
    assert [c.args[1] for c in created.call_args_list] == [
        "at(2026-10-10T10:30:05)",
        "at(2026-10-10T10:45:05)",
    ]


@pytest.mark.asyncio
async def test_one_instant_in_two_offsets_is_one_schedule(created):
    utc = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
    cdmx = utc.astimezone(timezone(timedelta(hours=-6)))
    first, second = await _ids(
        [
            {"operation": "reminder", "data": {"message": "Hi"}, "at": utc},
            {"operation": "reminder", "data": {"message": "Hi"}, "at": cdmx},
        ]
    )
    assert first == second


@pytest.mark.asyncio
async def test_invalid_cron_is_refused_before_anything_is_created(created):
    with pytest.raises(ValueError, match="too frequent"):
        await sched.schedule("reminder", PHONE, data={"m": "x"}, cron="0/5 * * * ? *")
    created.assert_not_called()


@pytest.mark.asyncio
async def test_two_reminders_with_the_same_text_both_exist(monkeypatch):
    """End to end against a mocked EventBridge Scheduler: both are listed."""
    monkeypatch.setattr(sched, "config", _ProdConfig())
    with mock_aws():
        monkeypatch.setattr(
            sched, "_scheduler_client", lambda: boto3.client("scheduler", "us-east-1")
        )
        for hour in (14, 23):
            await sched.schedule(
                "reminder",
                PHONE,
                data={"message": "Tomar pastilla"},
                at=datetime(2026, 10, 10, hour, 0, tzinfo=UTC),
            )
        listed = await sched.list_schedules(PHONE)
    assert sorted(s["expression"] for s in listed) == [
        "at(2026-10-10T14:00:00)",
        "at(2026-10-10T23:00:00)",
    ]


@pytest.mark.asyncio
async def test_two_reminders_with_the_same_delay_both_exist(monkeypatch, clock):
    monkeypatch.setattr(sched, "config", _ProdConfig())
    with mock_aws():
        monkeypatch.setattr(
            sched, "_scheduler_client", lambda: boto3.client("scheduler", "us-east-1")
        )
        for minute in (0, 15):
            clock.current = datetime(2026, 10, 10, 10, minute, 5, tzinfo=UTC)
            await sched.schedule(
                "reminder",
                PHONE,
                data={"message": "Revisar el horno"},
                delay_minutes=30,
            )
        listed = await sched.list_schedules(PHONE)
    assert sorted(s["expression"] for s in listed) == [
        "at(2026-10-10T10:30:05)",
        "at(2026-10-10T10:45:05)",
    ]


# ── time zones ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recurring_schedule_is_read_in_its_time_zone(monkeypatch):
    monkeypatch.setattr(sched, "config", _ProdConfig())
    with mock_aws():
        client = boto3.client("scheduler", "us-east-1")
        monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
        sid = await sched.schedule(
            "reminder",
            PHONE,
            data={"message": "Tomar agua"},
            cron="0 9 * * ? *",
            timezone="America/Mexico_City",
        )
        detail = client.get_schedule(Name=sched._schedule_name(sid))
        listed = await sched.list_schedules(PHONE)
    assert detail["ScheduleExpression"] == "cron(0 9 * * ? *)"
    assert detail["ScheduleExpressionTimezone"] == "America/Mexico_City"
    assert [(s["expression"], s["timezone"]) for s in listed] == [
        ("cron(0 9 * * ? *)", "America/Mexico_City")
    ]


@pytest.mark.asyncio
async def test_without_a_time_zone_a_schedule_stays_utc(created):
    await sched.schedule("reminder", PHONE, data={"m": "x"}, cron="0 15 * * ? *")
    assert created.call_args.kwargs["timezone"] is None


@pytest.mark.asyncio
async def test_a_naive_at_is_read_in_the_time_zone(created):
    await sched.schedule(
        "reminder",
        PHONE,
        data={"m": "x"},
        at=datetime(2026, 11, 2, 9, 0),
        timezone="America/New_York",
    )
    assert created.call_args.args[1] == "at(2026-11-02T09:00:00)"
    assert created.call_args.kwargs["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_an_aware_at_is_one_instant_whatever_the_time_zone(created):
    """An offset already fixes the instant; it is sent as UTC, as before."""
    fire_at = datetime(2026, 4, 14, 9, 0, tzinfo=timezone(timedelta(hours=-6)))
    await sched.schedule(
        "reminder", PHONE, data={"m": "x"}, at=fire_at, timezone="Asia/Tokyo"
    )
    assert created.call_args.args[1] == "at(2026-04-14T15:00:00)"
    assert created.call_args.kwargs["timezone"] is None


@pytest.mark.asyncio
async def test_the_same_cron_in_two_zones_is_two_schedules(created):
    call = {"operation": "reminder", "data": {"m": "x"}, "cron": "0 9 * * ? *"}
    ids = await _ids(
        [
            {**call, "timezone": "America/Mexico_City"},
            {**call, "timezone": "Europe/Madrid"},
            call,
        ]
    )
    assert len(set(ids)) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "Nowhere/Land",  # well formed, but no such zone
        "America",  # a directory of zones, not a zone
        "../../etc/passwd",
        "/etc/localtime",
        "America/Mexico_City; DROP",
        "America/Mexico City",
        "a" * 65,
        123,
    ],
)
@pytest.mark.asyncio
async def test_an_invalid_time_zone_is_refused(created, bad):
    with pytest.raises(ValueError, match="time zone"):
        await sched.schedule(
            "reminder", PHONE, data={"m": "x"}, cron="0 9 * * ? *", timezone=bad
        )
    created.assert_not_called()


@pytest.mark.asyncio
async def test_an_invalid_time_zone_is_refused_in_dev_mode_too():
    assert sched.config.is_dev_mode
    with pytest.raises(ValueError, match="time zone"):
        await sched.schedule(
            "reminder", PHONE, data={}, cron="0 9 * * ? *", timezone="Nowhere/Land"
        )


@pytest.mark.asyncio
async def test_without_a_tz_database_the_name_shape_still_guards(created, monkeypatch):
    """python:*-slim images can ship without tzdata, where zoneinfo knows no
    zone at all. A real zone name must still schedule (EventBridge validates
    it), and a malformed one must still be refused."""

    def no_database(key):
        raise sched.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(sched, "ZoneInfo", no_database)
    await sched.schedule(
        "reminder", PHONE, data={}, cron="0 9 * * ? *", timezone="America/Bogota"
    )
    assert created.call_args.kwargs["timezone"] == "America/Bogota"
    with pytest.raises(ValueError, match="time zone"):
        await sched.schedule(
            "reminder", PHONE, data={}, cron="0 9 * * ? *", timezone="../etc/x"
        )
