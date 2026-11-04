# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``socialagent_runtime.scheduler.schedule``: the per-user cap holds when
calls run at the same time.

Strands runs the tool calls of one model message concurrently, so one user
turn can make many ``schedule()`` calls at once. Each call counts the user's
schedules and then creates one. When the count and the create were separate
steps, every call counted the same existing schedules and passed the cap, and
one turn could create as many schedules as the model asked for.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import scheduler as sched  # noqa: E402

PHONE = "+525555550100"
CAP = 10


class _ProdConfig:
    """Deployed-mode config stub (the real one is a frozen singleton)."""

    is_dev_mode = False
    scheduler_prefix = "dev-bot"
    scheduler_lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:scheduler"
    has_scheduler_lambda = True
    scheduler_role_arn = "arn:aws:iam::123456789012:role/scheduler"
    region = "us-east-1"


class _FakeScheduler:
    """An in-memory EventBridge Scheduler, slow enough that calls overlap."""

    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        class ConflictException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, latency: float = 0.01) -> None:
        self.latency = latency
        self.schedules: dict[str, dict] = {}
        self._lock = threading.Lock()

    def list_schedules(self, NamePrefix: str = "", **_: object) -> dict:  # noqa: N803
        time.sleep(self.latency)
        with self._lock:
            names = [n for n in self.schedules if n.startswith(NamePrefix)]
        return {"Schedules": [{"Name": n} for n in names]}

    def get_schedule(self, Name: str) -> dict:  # noqa: N803
        time.sleep(self.latency)
        with self._lock:
            if Name not in self.schedules:
                raise self.exceptions.ResourceNotFoundException(Name)
            return self.schedules[Name]

    def create_schedule(self, **kwargs: object) -> None:
        time.sleep(self.latency)
        with self._lock:
            if kwargs["Name"] in self.schedules:
                raise self.exceptions.ConflictException(kwargs["Name"])
            self.schedules[kwargs["Name"]] = kwargs

    def update_schedule(self, **kwargs: object) -> None:
        time.sleep(self.latency)
        with self._lock:
            self.schedules[kwargs["Name"]] = kwargs


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeScheduler:
    client = _FakeScheduler()
    monkeypatch.setattr(sched, "config", _ProdConfig())
    monkeypatch.setattr(sched, "_MAX_SCHEDULES", CAP)
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    return client


def _requests(count: int, start: int = 0) -> list:
    """``count`` distinct recurring reminders, as one turn's parallel tool calls."""
    return [
        sched.schedule(
            "reminder",
            PHONE,
            data={"message": f"Reminder {i}"},
            cron=f"{i % 60} * * * ? *",
        )
        for i in range(start, start + count)
    ]


def _outcomes(results: list) -> tuple[int, int]:
    refused = [r for r in results if isinstance(r, sched.ScheduleLimitExceeded)]
    others = [r for r in results if isinstance(r, BaseException) and r not in refused]
    assert others == [], others
    return len(results) - len(refused), len(refused)


async def test_one_turns_parallel_calls_stop_at_the_cap(fake):
    results = await asyncio.gather(*_requests(15), return_exceptions=True)

    assert _outcomes(results) == (CAP, 5)
    assert len(fake.schedules) == CAP


def test_turns_on_two_event_loops_share_the_cap(fake):
    """Strands may run a turn on an event loop of its own, in a thread of its
    own, so the cap cannot rest on a lock that belongs to one loop."""
    results: list = []

    def turn(start: int) -> None:
        async def calls() -> list:
            return await asyncio.gather(*_requests(8, start), return_exceptions=True)

        results.extend(asyncio.run(calls()))

    threads = [threading.Thread(target=turn, args=(s,)) for s in (0, 8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _outcomes(results) == (CAP, 6)
    assert len(fake.schedules) == CAP


async def test_a_reschedule_at_the_cap_still_updates_in_place(fake):
    """The cap stops a new schedule, not a move of one the user already has."""
    sid = f"{sched._phone_digits(PHONE)}-checkin"
    await sched.schedule(
        "reminder", PHONE, data={"message": "a"}, schedule_id=sid, cron="0 9 * * ? *"
    )
    await asyncio.gather(*_requests(CAP - 1))

    results = await asyncio.gather(
        sched.schedule(
            "reminder",
            PHONE,
            data={"message": "b"},
            schedule_id=sid,
            cron="0 10 * * ? *",
        ),
        *_requests(3, start=CAP),
        return_exceptions=True,
    )

    assert _outcomes(results) == (1, 3)
    assert (
        fake.schedules[sched._schedule_name(sid)]["ScheduleExpression"]
        == "cron(0 10 * * ? *)"
    )
