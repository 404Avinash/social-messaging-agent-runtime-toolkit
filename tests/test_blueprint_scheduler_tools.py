# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The blueprints' scheduling tools scope every scheduler call to the caller.

``socialagent_runtime.scheduler`` decides ownership from the ``phone`` it is
given, so a tool wrapper that took the phone from a model-filled argument, or
dropped it, would undo the object-level authorization. Each wrapper must pass
``tool_context.invocation_state["phone"]`` and refuse to run without one.

The tools import ``src.*`` and ``socialagent_runtime`` as top-level packages, as
they do inside a scaffolded project, so each blueprint runs in a child
interpreter against a copy laid out that way, with the scheduler functions
replaced by recorders and AWS walled off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("strands")

REPO_ROOT = Path(__file__).resolve().parents[1]
PHONE = "+15551234567"

# Shared by both drivers: replace the scheduler's public functions with
# recorders that bind arguments against the real signatures, so positional and
# keyword calls are recorded the same way.
_PRELUDE = r"""
import asyncio
import inspect
import json
from datetime import datetime

import socialagent_runtime.scheduler as scheduler

calls = []


def _recorder(name, result):
    real = getattr(scheduler, name)

    async def fake(*args, **kwargs):
        bound = inspect.signature(real).bind(*args, **kwargs)
        bound.apply_defaults()
        # Scalars, datetimes and the payload dict; anything else (coroutines,
        # callables) is not JSON and not what these tests look at.
        calls.append({"fn": name, **{
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in bound.arguments.items()
            if isinstance(v, (str, int, dict, datetime, type(None)))
        }})
        return result

    setattr(scheduler, name, fake)


_recorder("schedule", "sid")
_recorder("cancel", None)
_recorder("list_schedules", [])


class Ctx:
    def __init__(self, state):
        self.invocation_state = state


def run(coro):
    return asyncio.run(coro)


def scheduler_tools(tools):
    # Every tool whose body calls the scheduler. A new one must be added to
    # the driver below, or the test's coverage check fails.
    return sorted(
        t.tool_name for t in tools
        if "scheduler." in inspect.getsource(t._tool_func)
    )
"""

_PERSONAL_ASSISTANT = (
    _PRELUDE
    + r"""
from src import tools as t

PHONE = "+15551234567"
ctx = Ctx({"phone": PHONE})
run(t.schedule_reminder(message="call mom", tool_context=ctx, delay_minutes=5))
run(t.schedule_task(task="weather", tool_context=ctx, delay_minutes=30))
run(t.schedule_recurring(operation="reminder", cron="0 9 * * ? *", tool_context=ctx, message="m"))
run(t.cancel_schedule(schedule_id="15551234567-reminder-abcd1234", tool_context=ctx))
run(t.list_schedules(tool_context=ctx))
exercised = ["cancel_schedule", "list_schedules", "schedule_recurring", "schedule_reminder", "schedule_task"]
with_phone = list(calls)

calls.clear()
empty = Ctx({})
refused = [
    run(t.schedule_reminder(message="m", tool_context=empty, delay_minutes=5)),
    run(t.schedule_task(task="m", tool_context=empty, delay_minutes=30)),
    run(t.schedule_recurring(operation="reminder", cron="0 9 * * ? *", tool_context=empty)),
    run(t.cancel_schedule(schedule_id="x", tool_context=empty)),
    run(t.list_schedules(tool_context=empty)),
]
print(json.dumps({
    "with_phone": with_phone,
    "without_phone": calls,
    "refused": refused,
    "scheduler_tools": scheduler_tools(t.socialagent_tools),
    "exercised": exercised,
}))
"""
)

_RECURRING = (
    _PRELUDE
    + r"""
from pathlib import Path

# A project skill that declares its own recurring operation.
Path("src/skills/checkin.md").write_text(
    "---\nname: checkin\ndescription: Daily check-in\noperations:\n"
    "  - name: daily_checkin\n    description: Ask how the day went\n"
    "    tool: schedule_recurring\n---\n\n# Check-in\n"
)

from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
requests = {
    "reminder": {"operation": "reminder", "message": "Take vitamins"},
    "task": {"operation": "invoke_agent", "message": "Write me a motivational quote",
             "timezone": "America/Mexico_City"},
    "declared": {"operation": "daily_checkin", "message": "How was your day?"},
    "invented": {"operation": "daily_weather", "message": "Weather"},
    "reminder_without_text": {"operation": "reminder"},
    "task_without_text": {"operation": "invoke_agent"},
}
results = {}
for label, kwargs in requests.items():
    calls.clear()
    out = run(t.schedule_recurring(cron="0 9 * * ? *", tool_context=ctx, **kwargs))
    results[label] = {"out": out, "calls": list(calls)}
print(json.dumps(results))
"""
)

# A cron whose year field is not "*" stops firing after that year.
_RECURRING_YEAR = (
    _PRELUDE
    + r"""
from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
crons = {
    "every_year": "0 9 * * ? *",
    "wrapped_every_year": "cron(0 9 1 1 ? *)",
    "one_date": "30 14 10 10 ? 2026",
    "wrapped_one_date": "cron(30 14 10 10 ? 2026)",
    "year_range": "0 9 * * ? 2026-2027",
    "year_list": "0 9 * * ? 2026,2027",
}
results = {}
for label, cron in crons.items():
    calls.clear()
    out = run(t.schedule_recurring(
        operation="invoke_agent", cron=cron, tool_context=ctx, message="Write a quote",
    ))
    results[label] = {"out": out, "calls": list(calls)}
print(json.dumps(results))
"""
)

_NOTHING_TO_DO = (
    _PRELUDE
    + r"""
from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
requests = {
    "task_empty": lambda: t.schedule_task(task="", tool_context=ctx, delay_minutes=30),
    "task_blank": lambda: t.schedule_task(task="  ", tool_context=ctx, delay_minutes=30),
    "recurring_blank": lambda: t.schedule_recurring(
        operation="invoke_agent", cron="0 9 * * ? *", tool_context=ctx, message="  "
    ),
    "reminder_empty": lambda: t.schedule_reminder(message="", tool_context=ctx, delay_minutes=30),
    "reminder_blank": lambda: t.schedule_reminder(
        message=" ", tool_context=ctx, at_datetime="2026-11-02T09:00:00-06:00"
    ),
}
results = {}
for label, request in requests.items():
    calls.clear()
    results[label] = {"out": run(request()), "calls": list(calls)}
print(json.dumps(results))
"""
)

_ONE_SHOT_TIMEZONE = (
    _PRELUDE
    + r"""
from datetime import timedelta

from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
# A task must still be ahead when the test runs, so its day is not fixed.
TASK_DAY = (datetime.now() + timedelta(days=30)).date().isoformat()
requests = {
    "reminder_local": lambda: t.schedule_reminder(
        message="Pick up your package!", tool_context=ctx,
        at_datetime="2026-11-02T09:00:00", timezone="America/New_York",
    ),
    "task_local": lambda: t.schedule_task(
        task="Write a motivational quote", tool_context=ctx,
        at_datetime=f"{TASK_DAY}T08:00:00", timezone="America/Mexico_City",
    ),
    "reminder_offset": lambda: t.schedule_reminder(
        message="Pick up your package!", tool_context=ctx,
        at_datetime="2026-11-02T09:00:00-05:00",
    ),
    "reminder_no_zone": lambda: t.schedule_reminder(
        message="Pick up your package!", tool_context=ctx, at_datetime="2026-11-02T09:00:00",
    ),
    "task_no_zone": lambda: t.schedule_task(
        task="Write a motivational quote", tool_context=ctx, at_datetime=f"{TASK_DAY}T08:00:00",
    ),
}
results = {"task_day": TASK_DAY}
for label, request in requests.items():
    calls.clear()
    results[label] = {"out": run(request()), "calls": list(calls)}
print(json.dumps(results))
"""
)

_OLDER_RUNTIME = (
    _PRELUDE
    + r"""
from datetime import UTC, timedelta

from src import tools as t


# The vendored runtime's schedule() before it took a time zone.
async def schedule(operation, phone, delay_minutes=0, data=None, schedule_id=None, at=None, *, cron=None):
    calls.append({"fn": "schedule", "operation": operation})
    return "sid"


t.scheduler.schedule = schedule
ctx = Ctx({"phone": "+15551234567"})
outs = [
    run(t.schedule_reminder(message="m", tool_context=ctx, delay_minutes=5)),
    run(t.schedule_reminder(message="m", tool_context=ctx, at_datetime="2026-11-02T09:00:00-05:00")),
    run(t.schedule_task(task="m", tool_context=ctx, at_datetime=(datetime.now(UTC) + timedelta(days=30)).isoformat())),
    run(t.schedule_recurring(operation="reminder", cron="0 9 * * ? *", tool_context=ctx, message="m")),
]
print(json.dumps({"outs": outs, "calls": calls}))
"""
)

_APPOINTMENT_SCHEDULER = (
    _PRELUDE
    + r"""
from src import tools as t


async def _noop(*args, **kwargs):
    return None


t.state.put = _noop
t.state.delete = _noop
t.resolve_property_by_name = lambda name: {"id": "p-1", "name": "Downtown Loft", "address": "12 River St", "lat": 1.0, "lng": 2.0}
t.resolve_slot_by_datetime = lambda property_id, date, time: {"date": "Friday", "time": "2:00 PM"}

PHONE = "+15551234567"
# cancel_visit cancels only when its own Yes/No question is pending and the current
# message is a yes; simulate the pending question so the cancel path runs.
import time as _time
BOOKING_NAME = "Downtown Loft"
async def _pending(phone, key):
    # the question cancel_visit recorded: fresh, about this booking, asked by another message
    return {"asked_at": int(_time.time()), "booking": [BOOKING_NAME, "", ""], "asked_by": ["m-ask"]} if key == "cancel_pending" else None
t.state.get = _pending
ctx = Ctx({"phone": PHONE, "booking": {"property": "Downtown Loft"}, "prompt": "yes", "message_type": "text", "message_id": "m-yes"})
run(t.execute_action(action_name="book_visit", params={"property_name": "Downtown Loft"}, tool_context=ctx))
run(t.execute_action(action_name="cancel_visit", params={}, tool_context=ctx))
exercised = ["execute_action"]
with_phone = list(calls)

calls.clear()
empty = Ctx({"booking": {"property": "Downtown Loft"}})
refused = [
    run(t.execute_action(action_name="book_visit", params={"property_name": "Downtown Loft"}, tool_context=empty)),
    run(t.execute_action(action_name="cancel_visit", params={}, tool_context=empty)),
]
print(json.dumps({
    "with_phone": with_phone,
    "without_phone": calls,
    "refused": refused,
    "scheduler_tools": scheduler_tools(t.tools),
    "exercised": exercised,
}))
"""
)


def _run_blueprint(tmp_path: Path, blueprint: str, driver: str) -> dict:
    project = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "blueprints" / blueprint / "src", project / "src")
    shutil.copytree(
        REPO_ROOT / "packages" / "socialagent-runtime",
        project / "socialagent_runtime",
    )
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "SOCIALAGENT_", "SCHEDULER_", "NOTIFICATION_"))
    }
    env.update(
        {
            "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
            "STATE_TABLE_NAME": "__socialagent_dev__",
            "TMPDIR": str(scratch),
            # Nothing here should reach AWS; if it tries, it fails locally.
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
            "AWS_REGION": "us-east-1",
        }
    )
    r = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _assert_scoped(out: dict) -> None:
    # Coverage: the driver exercised every tool that calls the scheduler.
    assert out["scheduler_tools"] == out["exercised"]
    # With a caller, every scheduler call carries the caller's own phone.
    assert out["with_phone"], "no scheduler calls were recorded"
    assert {c["phone"] for c in out["with_phone"]} == {PHONE}
    # Without one, nothing reaches the scheduler and each tool says why.
    assert out["without_phone"] == []
    assert all(r["status"] == "error" for r in out["refused"])


def test_personal_assistant_scheduler_tools_pass_the_caller_phone(tmp_path):
    out = _run_blueprint(tmp_path, "personal-assistant", _PERSONAL_ASSISTANT)
    _assert_scoped(out)
    by_fn = {c["fn"]: c for c in out["with_phone"]}
    assert set(by_fn) == {"schedule", "cancel", "list_schedules"}
    assert by_fn["cancel"]["schedule_id"] == "15551234567-reminder-abcd1234"


def test_appointment_scheduler_tools_pass_the_caller_phone(tmp_path):
    out = _run_blueprint(tmp_path, "appointment-scheduler", _APPOINTMENT_SCHEDULER)
    _assert_scoped(out)
    # The reminder id is derived from the caller's digits, never a tool argument.
    assert [(c["fn"], c["schedule_id"]) for c in out["with_phone"]] == [
        ("schedule", "reminder-15551234567"),
        ("cancel", "reminder-15551234567"),
    ]
    # The reminder runs in the scheduler Lambda, which does not ship src/, so
    # the location pin's inputs must travel in the payload or the pin is skipped.
    data = out["with_phone"][0]["data"]
    assert {"lat", "lng", "location_name", "location"} <= set(data), data


def test_a_booking_stored_before_the_rename_still_reads():
    """A booking saved by the car-era code has the key "car"; the context
    builder reads it for one release so a user mid-booking at deploy time is
    not stuck. agent.py calls app.run() at import, so only the helper is loaded."""
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "blueprints"
        / "appointment-scheduler"
        / "agent.py"
    ).read_text()
    start = src.index("def _build_context")
    end = src.index("\ndef ", start + 10)
    ns: dict = {}
    exec(src[start:end], ns)  # noqa: S102
    base = {"date": "Friday, July 17", "time": "2:00 PM", "location": "x"}
    assert "Downtown Loft" in ns["_build_context"]({"car": "Downtown Loft", **base})
    assert "Downtown Loft" in ns["_build_context"](
        {"property": "Downtown Loft", **base}
    )


_CANCEL_OLD_BOOKING = (
    _PRELUDE
    + r"""
from src import tools as t


async def _noop(*args, **kwargs):
    return None


t.state.delete = _noop
import time as _time
BOOKING_NAME = "Garden House"
async def _pending(phone, key):
    # the question cancel_visit recorded: fresh, about this booking, asked by another message
    return {"asked_at": int(_time.time()), "booking": [BOOKING_NAME, "", ""], "asked_by": ["m-ask"]} if key == "cancel_pending" else None
t.state.get = _pending
ctx = Ctx({"phone": "+15551234567", "booking": {"car": "Garden House"}, "prompt": "[Button reply: Yes, cancel]", "message_type": "button_reply"})
out = run(t.execute_action(action_name="cancel_visit", params={}, tool_context=ctx))
print(json.dumps({"out": out}))
"""
)


def test_cancel_visit_reads_a_pre_rename_booking(tmp_path):
    """A booking stored with "car" before the rename cancels and is named."""
    out = _run_blueprint(tmp_path, "appointment-scheduler", _CANCEL_OLD_BOOKING)
    assert out["out"] == {"status": "cancelled", "property": "Garden House"}


def _visit_reminder_handler(monkeypatch):
    """The appointment-scheduler blueprint's visit_reminder handler, with no sleeps."""
    import importlib.util

    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
    }.items():
        monkeypatch.setenv(k, v)
    handlers_dir = REPO_ROOT / "blueprints" / "appointment-scheduler" / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_sched_probe", handlers_dir / "scheduler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod.app._schedule_handlers["visit_reminder"]


class _NoopState:
    """``ctx.state`` for a reminder test that does not look at the cancel question."""

    async def delete(self, phone, key):
        return None

    async def put(self, phone, key, data, ttl_minutes=None):
        return None


def test_the_visit_reminder_clears_the_cancel_question_before_it_sends_and_records_its_delivery_after(
    monkeypatch,
):
    """The replies a reminder invites ("Sí, gracias", "Yes, thanks", "Yes") are on
    the cancel tool's closed list of typed yeses, so a cancel question still open
    when the reminder arrives would read a confirmation of attendance as a
    cancellation. The handler deletes it first, then sends, and the moment the
    template is out records when it delivered and for which booking
    (`reminder_sent`, keyed as the tool keys a booking): EventBridge Scheduler has
    no delivery SLA, so the scheduled moment is not the delivered one, and a marker
    written before the send would carry the time of an attempt, not of a
    delivery."""
    import asyncio
    import time

    handler = _visit_reminder_handler(monkeypatch)
    calls: list = []

    class FakeState:
        async def delete(self, phone, key):
            calls.append(("delete", phone, key))

        async def put(self, phone, key, data, ttl_minutes=None):
            calls.append(("put", phone, key, data, ttl_minutes))

    class FakeCtx:
        state = FakeState()

        async def send_template(self, phone, name, params, language="en"):
            calls.append(("template", phone, name))

        async def send_message(self, phone, msg, memory_text=""):
            calls.append(("message", phone))

        async def write_to_memory(self, phone, text):
            calls.append(("memory", phone))

    before = int(time.time())
    asyncio.run(
        handler(
            {
                "phone": "+15555550100",
                "property": "Garden House",
                "date": "Thursday, October 29",
                "time": "10:00 AM",
                "location": "48 Elm Ave",
                "lat": 19.4178,
                "lng": -99.1838,
            },
            FakeCtx(),
        )
    )
    put = calls[2]
    assert put[:3] == ("put", "+15555550100", "reminder_sent") and put[4] == 180
    assert put[3]["booking"] == ["Garden House", "Thursday, October 29", "10:00 AM"]
    assert before <= put[3]["at"] <= int(time.time())
    assert isinstance(put[3]["at"], int)
    assert [c[:3] for c in calls] == [
        ("delete", "+15555550100", "cancel_pending"),
        ("template", "+15555550100", "visit_reminder"),
        ("put", "+15555550100", "reminder_sent"),
        ("message", "+15555550100"),
        ("memory", "+15555550100"),
    ]


def test_the_visit_reminder_records_its_delivery_right_after_the_template_goes_out(
    monkeypatch,
):
    """The marker must follow the template send directly, before the ten-second
    pause ahead of the location pin and before the memory note: the user has the
    reminder the moment the template is out, and until the marker exists a typed
    "Sí, gracias" processed in that gap meets no marker. A reminder more than two
    minutes late, delivered inside the asking turn before the tool read state,
    then has nothing to close the question with, and the reply cancels the visit."""
    import asyncio

    handler = _visit_reminder_handler(monkeypatch)
    calls: list = []

    class FakeState:
        async def delete(self, phone, key):
            calls.append("delete")

        async def put(self, phone, key, data, ttl_minutes=None):
            calls.append("put")

    class FakeCtx:
        state = FakeState()

        async def send_template(self, phone, name, params, language="en"):
            calls.append("template")

        async def send_message(self, phone, msg, memory_text=""):
            calls.append("message")

        async def write_to_memory(self, phone, text):
            calls.append("memory")

    asyncio.run(
        handler(
            {
                "phone": "+15555550100",
                "property": "Garden House",
                "date": "Thursday, October 29",
                "time": "10:00 AM",
                "location": "48 Elm Ave",
                "lat": 19.4178,
                "lng": -99.1838,
            },
            FakeCtx(),
        )
    )
    assert calls == ["delete", "template", "put", "message", "memory"]


_REMINDER_PAYLOAD = {
    "phone": "+15555550100",
    "property": "Garden House",
    "date": "Thursday, October 29",
    "time": "10:00 AM",
    "location": "48 Elm Ave",
}


def test_the_visit_reminder_survives_a_failed_delivery_marker(monkeypatch, caplog):
    """The marker is best-effort: a state write that fails after the template is
    out logs a warning and the handler returns, so a retried invocation does not
    send the reminder again. The tool then falls back to the fixed window."""
    import asyncio
    import logging

    handler = _visit_reminder_handler(monkeypatch)
    calls: list = []

    class FailingPutState(_NoopState):
        async def put(self, phone, key, data, ttl_minutes=None):
            calls.append("put")
            raise RuntimeError("ProvisionedThroughputExceededException")

    class FakeCtx:
        state = FailingPutState()

        async def send_template(self, phone, name, params, language="en"):
            calls.append("template")

        async def write_to_memory(self, phone, text):
            calls.append("memory")

    with caplog.at_level(logging.WARNING):
        asyncio.run(handler(_REMINDER_PAYLOAD, FakeCtx()))
    assert calls == ["template", "put", "memory"]
    assert any(
        r.levelno == logging.WARNING and "delivery marker failed" in r.getMessage()
        for r in caplog.records
    )


def test_the_visit_reminder_writes_no_marker_when_the_template_send_fails(
    monkeypatch,
):
    """A send that raises is no delivery: the marker is not written (the tool's
    fixed window then stands in) and the error propagates, so the invocation is
    retried."""
    import asyncio

    handler = _visit_reminder_handler(monkeypatch)
    calls: list = []

    class RecordingState(_NoopState):
        async def put(self, phone, key, data, ttl_minutes=None):
            calls.append("put")

    class FailingSendCtx:
        state = RecordingState()

        async def send_template(self, phone, name, params, language="en"):
            calls.append("template")
            raise RuntimeError("ThrottlingException")

        async def write_to_memory(self, phone, text):
            calls.append("memory")

    with pytest.raises(RuntimeError, match="ThrottlingException"):
        asyncio.run(handler(_REMINDER_PAYLOAD, FailingSendCtx()))
    assert calls == ["template"]


def test_scheduler_handler_reads_a_pre_rename_payload_and_pins_the_coordinates(
    monkeypatch,
):
    """A reminder scheduled before the rename carries "car" and no date; the
    handler sends the template and a location pin with the payload's coordinates,
    not just something with the right keys, and its delivery marker names no
    booking, which the tool reads as the current one."""
    import asyncio

    handler = _visit_reminder_handler(monkeypatch)
    sent: list = []

    class FakeState(_NoopState):
        async def put(self, phone, key, data, ttl_minutes=None):
            sent.append(("put", key, data["booking"]))

    class FakeCtx:
        state = FakeState()

        async def send_template(self, phone, name, params, language="en"):
            sent.append(("template", list(params)))

        async def send_message(self, phone, msg, memory_text=""):
            sent.append(("message", msg.payload))

        async def write_to_memory(self, phone, text):
            sent.append(("memory", text))

    old_payload = {
        "phone": "+15555550100",
        "car": "Garden House",
        "time": "10:00 AM",
        "location": "48 Elm Ave, Your City",
        "location_name": "Garden House",
        "lat": 19.4178,
        "lng": -99.1838,
    }
    asyncio.run(handler(old_payload, FakeCtx()))
    assert sent[0] == (
        "template",
        ["Garden House", "10:00 AM", "48 Elm Ave, Your City"],
    )
    assert sent[1] == ("put", "reminder_sent", None)
    kind, payload = sent[2]
    assert kind == "message"
    loc = payload["location"]
    assert (loc["latitude"], loc["longitude"]) == (19.4178, -99.1838)
    assert loc["name"] == "Garden House"
    assert "Garden House" in sent[3][1]


def test_visit_reminder_survives_a_failed_memory_note(monkeypatch):
    """A memory-note failure after the reminder is sent must not propagate.

    The run would fail and its event go to the DLQ, as if the user had got no
    reminder.
    """
    import asyncio

    handler = _visit_reminder_handler(monkeypatch)
    sent: list = []

    class FailingMemoryCtx:
        state = _NoopState()

        async def send_template(self, phone, name, params, language="en"):
            sent.append("template")

        async def write_to_memory(self, phone, text):
            raise RuntimeError("ThrottledException")

    asyncio.run(
        handler(
            {
                "phone": "+15555550100",
                "property": "Garden House",
                "time": "10:00 AM",
                "location": "48 Elm Ave",
            },
            FailingMemoryCtx(),
        )
    )
    assert sent == ["template"]


def test_visit_reminder_follows_the_booking_language(monkeypatch):
    """A booking made in Spanish schedules its reminder with ``language: es``; the
    handler then sends the Spanish template. Without the key (older schedules)
    it stays English."""
    import asyncio

    handler = _visit_reminder_handler(monkeypatch)
    sent: list = []

    class FakeCtx:
        state = _NoopState()

        async def send_template(self, phone, name, params, language="en"):
            sent.append((name, language))

        async def send_message(self, phone, msg, memory_text=""):
            pass

        async def write_to_memory(self, phone, text):
            pass

    base = {
        "phone": "+15555550100",
        "property": "Garden House",
        "time": "10:00 AM",
        "location": "48 Elm Ave",
    }
    asyncio.run(handler({**base, "language": "es"}, FakeCtx()))
    asyncio.run(handler(base, FakeCtx()))
    assert sent == [("visit_reminder_es", "es_MX"), ("visit_reminder", "en")]


_BOOK_LANGUAGE = (
    _PRELUDE
    + r"""
from src import tools as t

stored = {}
async def put(phone, key, value, ttl_minutes=None): stored[key] = value
async def delete(phone, key): pass
t.state.put = put
t.state.delete = delete
t.resolve_property_by_name = lambda name: {"id": "p-1", "name": "Downtown Loft", "address": "12 River St", "lat": 1.0, "lng": 2.0}
t.resolve_slot_by_datetime = lambda property_id, date, time: {"date": "Thursday, October 29", "time": "9:00 AM"}

def book(prompt):
    # the state agent.py builds for a first booking: no booking, so the default language is en
    ctx = Ctx({"phone": "+15551234567", "booking": None, "prompt": prompt, "language": "en"})
    run(t.execute_action(action_name="book_visit", params={"property_name": "Downtown Loft"}, tool_context=ctx))
    return stored["booking"]["language"], calls[-1]["data"]["language"]

print(json.dumps({"tap_si": book("[Button reply: Sí]"), "dale": book("dale"), "tap_yes": book("[Button reply: Yes]"),
                  "loanword": book("Yes, gracias!")}))
"""
)


def test_personal_assistant_recurring_schedules_reach_a_handler(tmp_path):
    """schedule_recurring creates only operations a handler serves, with the
    data field that handler reads.

    It took any well-formed name and always stored the text as ``message``:
    a recurring ``invoke_agent`` reached its handler, which reads ``prompt``,
    with nothing to do, and an invented operation fired a 400 on every run
    while holding one of the user's schedule slots. So does an operation a
    skill declares with no handler behind it, so a declaration alone does not
    make the tool accept one.
    """
    out = _run_blueprint(tmp_path, "personal-assistant", _RECURRING)

    def scheduled(label: str) -> dict:
        assert out[label]["out"]["status"] == "scheduled", out[label]
        [call] = out[label]["calls"]
        return call

    reminder = scheduled("reminder")
    assert (reminder["operation"], reminder["data"]) == (
        "reminder",
        {"message": "Take vitamins"},
    )
    assert reminder["timezone"] is None

    task = scheduled("task")
    assert (task["operation"], task["data"]) == (
        "invoke_agent",
        {"prompt": "Write me a motivational quote"},
    )
    assert task["timezone"] == "America/Mexico_City"
    assert out["task"]["out"]["timezone"] == "America/Mexico_City"

    for label in (
        "declared",
        "invented",
        "reminder_without_text",
        "task_without_text",
    ):
        assert out[label]["out"]["status"] == "error", out[label]
        assert out[label]["calls"] == []
    assert "daily_checkin" in out["declared"]["out"]["message"]
    assert "daily_weather" in out["invented"]["out"]["message"]
    for label in ("declared", "invented"):
        assert "invoke_agent" in out[label]["out"]["message"]
        assert "reminder" in out[label]["out"]["message"]


def test_personal_assistant_recurring_schedules_repeat_every_year(tmp_path):
    """A recurring cron with a year in it fires only in the years it names,
    but stays a recurring schedule: it is never deleted, and holds one of the
    user's schedule slots until it is cancelled. schedule_recurring takes
    only a year field of "*"."""
    out = _run_blueprint(tmp_path, "personal-assistant", _RECURRING_YEAR)

    for label in ("every_year", "wrapped_every_year"):
        assert out[label]["out"]["status"] == "scheduled", out[label]
        assert len(out[label]["calls"]) == 1, label
    for label in ("one_date", "wrapped_one_date", "year_range", "year_list"):
        assert out[label]["out"]["status"] == "error", out[label]
        assert out[label]["calls"] == [], label
        assert "schedule_task" in out[label]["out"]["message"], label


def test_personal_assistant_refuses_a_schedule_with_nothing_to_do(tmp_path):
    """A task with no text fires only to tell the user it could not run, and a
    reminder with none sends an empty template parameter that Meta rejects,
    then a blank fallback text, so both are refused before a schedule exists."""
    out = _run_blueprint(tmp_path, "personal-assistant", _NOTHING_TO_DO)
    for label, result in out.items():
        assert result["out"]["status"] == "error", label
        assert result["calls"] == [], label


def test_personal_assistant_one_shot_schedules_take_the_users_time_zone(tmp_path):
    """A one-shot time in the user's zone fires at that local time, the zone's
    offset on that date applied by EventBridge.

    schedule_reminder and schedule_task took no time zone, so the model had
    to work out the UTC offset in force on a future date itself, and a time
    sent without one was read as UTC: a 9am reminder in Mexico City fired at
    3am, reported as scheduled.
    """
    out = _run_blueprint(tmp_path, "personal-assistant", _ONE_SHOT_TIMEZONE)

    def scheduled(label: str) -> dict:
        assert out[label]["out"]["status"] == "scheduled", out[label]
        [call] = out[label]["calls"]
        return call

    reminder = scheduled("reminder_local")
    assert (reminder["at"], reminder["timezone"]) == (
        "2026-11-02T09:00:00",
        "America/New_York",
    )
    task = scheduled("task_local")
    assert (task["at"], task["timezone"]) == (
        f"{out['task_day']}T08:00:00",
        "America/Mexico_City",
    )
    offset = scheduled("reminder_offset")
    assert (offset["at"], offset["timezone"]) == ("2026-11-02T09:00:00-05:00", None)

    for label in ("reminder_no_zone", "task_no_zone"):
        assert out[label]["out"]["status"] == "error", out[label]
        assert out[label]["calls"] == [], label
        assert "timezone" in out[label]["out"]["message"]


def test_personal_assistant_schedules_without_a_zone_on_an_older_runtime(tmp_path):
    """A project that takes the new tools before replacing its vendored
    runtime still schedules everything that names no time zone."""
    out = _run_blueprint(tmp_path, "personal-assistant", _OLDER_RUNTIME)
    assert [o["status"] for o in out["outs"]] == ["scheduled"] * 4
    assert len(out["calls"]) == 4


# The session the driver runs in is set first, by the test.
_IN_A_SESSION = (
    _PRELUDE
    + r"""
from bedrock_agentcore.runtime import BedrockAgentCoreContext

from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
requests = {
    "reminder": lambda: t.schedule_reminder(message="Call mom", tool_context=ctx, delay_minutes=30),
    "task": lambda: t.schedule_task(task="Write a quote", tool_context=ctx, delay_minutes=30),
    "recurring": lambda: t.schedule_recurring(
        operation="invoke_agent", cron="0 9 * * ? *", tool_context=ctx, message="Write a quote"
    ),
    "list": lambda: t.list_schedules(tool_context=ctx),
    "cancel": lambda: t.cancel_schedule(schedule_id="15551234567-reminder-abcd1234", tool_context=ctx),
}
results = {}
for session, sid in SESSIONS.items():
    BedrockAgentCoreContext.set_request_context("request-1", sid)
    results[session] = {}
    for label, request in requests.items():
        calls.clear()
        results[session][label] = {"out": run(request()), "calls": [c["fn"] for c in calls]}
print(json.dumps(results))
"""
)


def test_book_visit_stores_the_language_of_the_confirming_message(tmp_path):
    out = _run_blueprint(tmp_path, "appointment-scheduler", _BOOK_LANGUAGE)
    assert out["tap_si"] == ["es", "es"]
    assert out["dale"] == ["es", "es"]
    assert out["tap_yes"] == ["en", "en"]
    assert out["loanword"] == ["en", "en"]


def _scheduled_task_session(monkeypatch: pytest.MonkeyPatch) -> str:
    """The AgentCore session personal-assistant's invoke_agent handler runs a task in."""
    import asyncio
    import importlib.util
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc",
    )
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = REPO_ROOT / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("pa_scheduler_session", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sessions = []

    def invoke_runtime(arn, session_id, payload, region, read_timeout):
        sessions.append(session_id)
        return {"result": "done"}

    monkeypatch.setattr(module, "invoke_runtime", invoke_runtime)
    ctx = MagicMock()
    ctx.send_template = AsyncMock()
    ctx.send_text = AsyncMock()
    ctx.lambda_context = None
    data = {
        "phone": PHONE,
        "prompt": "Write a quote",
        "schedule_id": "15551234567-invoke_agent-abcd1234",
    }
    asyncio.run(module.handle_invoke_agent(data, ctx))
    [session] = sessions
    return session


def test_personal_assistant_scheduled_task_creates_no_schedules(tmp_path, monkeypatch):
    """A scheduled task runs with every tool, so it could schedule itself again
    a minute later, every time it ran: agent turns and templates with no
    message from the user, past the webhook's rate limit and the 15-minute
    cron floor. Inside the session the invoke_agent handler runs a task in,
    the tools that create schedules refuse, and listing and cancelling still
    work."""
    sessions = {
        "scheduled": _scheduled_task_session(monkeypatch),
        "conversation": "whatsapp-session-conversation-15551234567",
        "none": None,
    }
    out = _run_blueprint(
        tmp_path, "personal-assistant", f"SESSIONS = {sessions!r}\n" + _IN_A_SESSION
    )

    task = out["scheduled"]
    for label in ("reminder", "task", "recurring"):
        assert task[label]["out"]["status"] == "error", task[label]
        assert "scheduled task" in task[label]["out"]["message"]
        assert task[label]["calls"] == [], label
    assert task["list"]["calls"] == ["list_schedules"]
    assert task["cancel"]["calls"] == ["cancel"]
    for session in ("conversation", "none"):
        for label in ("reminder", "task", "recurring"):
            assert out[session][label]["out"]["status"] == "scheduled", (
                session,
                out[session][label],
            )


# The model calls one tool, then replies with the tool's result.
_THROUGH_THE_RUNTIME = (
    _PRELUDE
    + r"""
import httpx
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import Model

from src import tools as t


class OneToolCall(Model):
    def __init__(self):
        self.turns = 0

    def update_config(self, **_):
        pass

    def get_config(self):
        return {}

    def structured_output(self, *args, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.turns += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.turns == 1:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tool-1", "name": "schedule_task"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"task": "Write a quote", "delay_minutes": 30})}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockDelta": {"delta": {"text": json.dumps(messages[-1]["content"])}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


app = BedrockAgentCoreApp()


# As agent.py's entrypoint runs a turn, without the session manager.
@app.entrypoint
async def invoke(payload, context=None):
    agent = Agent(model=OneToolCall(), tools=t.socialagent_tools, callback_handler=None)
    result = agent(payload["prompt"], invocation_state={"phone": payload["phone"]})
    return {"result": str(result)}


async def post(session):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.post(
            "/invocations",
            json={"prompt": "Do it", "phone": "+15551234567"},
            headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session},
        )
    return resp.status_code, resp.json()


results = {}
for label, session in SESSIONS.items():
    calls.clear()
    status, body = run(post(session))
    results[label] = {"status": status, "result": body["result"], "calls": [c["fn"] for c in calls]}
print(json.dumps(results))
"""
)


def test_personal_assistant_tools_see_the_session_agentcore_sends(
    tmp_path, monkeypatch
):
    """AgentCore passes the runtime session id in a request header, and the
    tools read it through the turn: BedrockAgentCoreApp runs the entrypoint
    on its own event loop and Strands runs the tool on another thread, each
    with a copy of the request's context."""
    sessions = {
        "scheduled": _scheduled_task_session(monkeypatch),
        "conversation": "whatsapp-session-conversation-15551234567",
    }
    out = _run_blueprint(
        tmp_path,
        "personal-assistant",
        f"SESSIONS = {sessions!r}\n" + _THROUGH_THE_RUNTIME,
    )

    assert out["scheduled"]["status"] == 200
    assert out["scheduled"]["calls"] == []
    assert "cannot create schedules" in out["scheduled"]["result"]
    assert out["conversation"]["status"] == 200
    assert out["conversation"]["calls"] == ["schedule"]


_TASK_LEAD_TIME = (
    _PRELUDE
    + r"""
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from src import tools as t

ctx = Ctx({"phone": "+15551234567"})
now = datetime.now(UTC)
# The wall-clock time in Mexico City, as the agent writes it with timezone set.
local = now.astimezone(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)
ZONE = "America/Mexico_City"
# The same in the zone with the largest UTC offset, UTC+14 all year.
EAST = "Pacific/Kiritimati"
east = now.astimezone(ZoneInfo(EAST)).replace(tzinfo=None)


def stamp(dt):
    return dt.replace(microsecond=0).isoformat()


def task(**timing):
    return lambda: t.schedule_task(task="Write a quote", tool_context=ctx, **timing)


requests = {
    "no_timing": task(),
    "delay_14": task(delay_minutes=14),
    "delay_15": task(delay_minutes=15),
    "offset_10": task(at_datetime=stamp(now + timedelta(minutes=10))),
    "offset_20": task(at_datetime=stamp(now + timedelta(minutes=20))),
    "past": task(at_datetime=stamp(now - timedelta(days=1))),
    "local_10": task(at_datetime=stamp(local + timedelta(minutes=10)), timezone=ZONE),
    "local_20": task(at_datetime=stamp(local + timedelta(minutes=20)), timezone=ZONE),
    "unknown_zone_10": task(at_datetime=stamp(local + timedelta(minutes=10)), timezone="Mars/Olympus"),
    "unknown_zone_2_days": task(at_datetime=stamp(local + timedelta(days=2)), timezone="Mars/Olympus"),
    "reminder_delay_1": lambda: t.schedule_reminder(message="Stretch", tool_context=ctx, delay_minutes=1),
    "reminder_local_2": lambda: t.schedule_reminder(
        message="Stretch", tool_context=ctx, at_datetime=stamp(local + timedelta(minutes=2)), timezone=ZONE
    ),
}
results = {}
for label, request in requests.items():
    calls.clear()
    results[label] = {"out": run(request()), "calls": list(calls)}


# An image with no tz database cannot read the zone.
def no_zone(name):
    raise t.ZoneInfoNotFoundError(name)


t.ZoneInfo = no_zone
unread = {
    "unread_local_20": task(at_datetime=stamp(local + timedelta(minutes=20)), timezone=ZONE),
    "unread_local_2_days": task(at_datetime=stamp(local + timedelta(days=2)), timezone=ZONE),
    "unread_east_10": task(at_datetime=stamp(east + timedelta(minutes=10)), timezone=EAST),
    "unread_east_20": task(at_datetime=stamp(east + timedelta(minutes=20)), timezone=EAST),
    "unread_offset_20": task(at_datetime=stamp(now + timedelta(minutes=20))),
}
for label, request in unread.items():
    calls.clear()
    results[label] = {"out": run(request()), "calls": list(calls)}
print(json.dumps(results))
"""
)


def test_personal_assistant_task_needs_the_cron_floor_as_notice(tmp_path):
    """A one-shot task is an agent turn and a template the user did not ask
    for in that minute, so it fires no sooner than a recurring one may:
    MIN_CRON_INTERVAL_MINUTES from now. A reminder only sends text and keeps
    any delay.

    A one-shot delay had no minimum, so tasks could be spaced a minute apart,
    each one asking for the next, past the cron floor.
    """
    out = _run_blueprint(tmp_path, "personal-assistant", _TASK_LEAD_TIME)

    for label in ("no_timing", "delay_14", "offset_10", "past", "local_10"):
        assert out[label]["out"]["status"] == "error", (label, out[label])
        assert "15 minutes" in out[label]["out"]["message"], label
        assert out[label]["calls"] == [], label
    for label in (
        "delay_15",
        "offset_20",
        "local_20",
        "reminder_delay_1",
        "reminder_local_2",
    ):
        assert out[label]["out"]["status"] == "scheduled", (label, out[label])
        assert len(out[label]["calls"]) == 1, label

    # A zone the tz database does not have gets the scheduler's message, near
    # or far: it was read as UTC+14, so a far time reached the scheduler and
    # raised out of the tool, and a near one was told to give its UTC offset.
    for label in ("unknown_zone_10", "unknown_zone_2_days"):
        assert out[label]["out"]["status"] == "error", (label, out[label])
        message = out[label]["out"]["message"]
        assert "unknown time zone: 'Mars/Olympus'" in message, label
        assert out[label]["calls"] == [], label

    # Without a tz database the zone's offset is unknown: a time that is at
    # least 15 minutes away at any offset is taken, and a nearer one must say
    # its offset instead. The east cases pin the offset assumed to UTC+14: at
    # a smaller one, 10 minutes away in Kiritimati would be hours away.
    for label in ("unread_local_20", "unread_east_10"):
        assert out[label]["out"]["status"] == "error", (label, out[label])
        assert "UTC offset" in out[label]["out"]["message"], label
        assert out[label]["calls"] == [], label
    for label in ("unread_local_2_days", "unread_offset_20", "unread_east_20"):
        assert out[label]["out"]["status"] == "scheduled", (label, out[label])
