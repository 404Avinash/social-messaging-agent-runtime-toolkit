# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for EventBridge scheduler (schedule_message, cancel_schedule)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from socialagent.scheduler.eventbridge import (
    _at_expression,
    _message_to_payload,
    _safe_name,
    _schedule_name,
    cancel_schedule,
    schedule_message,
    schedule_operation,
)
from socialagent.types import OutboundMessage


# ── Pure helpers ───────────────────────────────────────────────────────────────


def test_safe_name_replaces_special_chars():
    """Non-alphanumeric characters are replaced with hyphens."""
    assert _safe_name("+52 556/047") == "-52-556-047"


def test_safe_name_leaves_alphanumeric_and_hyphens():
    """Alphanumeric characters and hyphens are preserved (lowercased)."""
    assert _safe_name("order-123-ABC") == "order-123-abc"


def test_schedule_name_with_prefix():
    """Schedule name is prefix-safe_id when prefix is provided."""
    assert _schedule_name("prod", "order-123") == "prod-order-123"


def test_schedule_name_without_prefix():
    """Schedule name is just safe_id when prefix is empty."""
    assert _schedule_name("", "order-123") == "order-123"


def test_schedule_name_truncation_is_stable():
    """Names over 64 chars are cut to 56 and get a 7-char md5 suffix.

    The suffix is part of every long schedule's identity in EventBridge, so a
    change to the hashing would orphan schedules that already exist.
    """
    name = _schedule_name("socialagent-dev", "reminder-" + "x" * 80)
    assert name == "socialagent-dev-reminder-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-ca6ca14"
    assert len(name) == 64


def test_at_expression_format():
    """at() expression uses UTC time formatted correctly."""
    expr = _at_expression(60)
    assert expr.startswith("at(")
    assert expr.endswith(")")
    # Should be roughly 60 minutes from now
    inner = expr[3:-1]
    parsed = datetime.fromisoformat(inner).replace(tzinfo=UTC)
    delta = parsed - datetime.now(UTC)
    assert 55 <= delta.total_seconds() / 60 <= 65


def test_at_expression_zero_delay():
    """Zero delay produces an expression close to now."""
    expr = _at_expression(0)
    inner = expr[3:-1]
    parsed = datetime.fromisoformat(inner).replace(tzinfo=UTC)
    delta = abs((parsed - datetime.now(UTC)).total_seconds())
    assert delta < 5


def test_message_to_payload_text():
    """Plain text message serializes to type=text."""
    result = _message_to_payload("Hello")
    assert result == {"type": "text", "text": "Hello"}


def test_message_to_payload_outbound():
    """OutboundMessage serializes to type + payload."""
    msg = OutboundMessage(type="interactive", payload={"key": "val"})
    result = _message_to_payload(msg)
    assert result == {"type": "interactive", "payload": {"key": "val"}}


# ── schedule_message ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_message_calls_create_schedule():
    """schedule_message calls boto3 create_schedule with correct args."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        name = await schedule_message(
            schedule_id="order-123",
            phone="+521234567890",
            message="Hola",
            delay_minutes=60,
            target_arn="arn:aws:lambda:us-east-1:123:function:notify",
            role_arn="arn:aws:iam::123:role/scheduler",
        )

    assert name == "order-123"
    mock_client.create_schedule.assert_called_once()
    kwargs = mock_client.create_schedule.call_args.kwargs
    assert kwargs["Name"] == "order-123"
    assert kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert kwargs["ActionAfterCompletion"] == "DELETE"
    assert kwargs["Target"]["Arn"] == "arn:aws:lambda:us-east-1:123:function:notify"
    assert kwargs["Target"]["RoleArn"] == "arn:aws:iam::123:role/scheduler"


@pytest.mark.asyncio
async def test_schedule_message_with_prefix():
    """schedule_message returns the schedule_id (not the prefixed name)."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        sid = await schedule_message(
            schedule_id="order-123",
            phone="+521234567890",
            message="Hola",
            delay_minutes=30,
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            prefix="prod",
        )

    assert sid == "order-123"
    kwargs = mock_client.create_schedule.call_args.kwargs
    assert kwargs["Name"] == "prod-order-123"


@pytest.mark.asyncio
async def test_schedule_message_upserts_on_conflict():
    """schedule_message calls update_schedule when create raises ConflictException."""
    mock_client = MagicMock()

    class ConflictException(Exception):  # noqa: N818
        pass

    mock_client.exceptions.ConflictException = ConflictException
    mock_client.create_schedule.side_effect = ConflictException("exists")

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="order-123",
            phone="+521234567890",
            message="Hola",
            delay_minutes=10,
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
        )

    mock_client.update_schedule.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_message_input_payload_structure():
    """Target Input payload contains source, phone, message, schedule_id."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="rem-1",
            phone="+521234567890",
            message="Reminder",
            delay_minutes=5,
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
        )

    raw_input = mock_client.create_schedule.call_args.kwargs["Target"]["Input"]
    payload = json.loads(raw_input)
    assert payload["source"] == "socialagent_scheduler"
    assert payload["phone"] == "+521234567890"
    assert payload["schedule_id"] == "rem-1"
    assert payload["message"] == {"type": "text", "text": "Reminder"}


@pytest.mark.asyncio
async def test_schedule_message_outbound_message_payload():
    """OutboundMessage is serialized correctly into the target input."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    msg = OutboundMessage(type="template", payload={"name": "welcome"})

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="s1",
            phone="+521234567890",
            message=msg,
            delay_minutes=1,
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
        )

    raw_input = mock_client.create_schedule.call_args.kwargs["Target"]["Input"]
    payload = json.loads(raw_input)
    assert payload["message"] == {"type": "template", "payload": {"name": "welcome"}}


# ── cancel_schedule ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_schedule_calls_delete():
    """cancel_schedule calls delete_schedule with the correct name."""
    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await cancel_schedule("order-123")

    mock_client.delete_schedule.assert_called_once_with(Name="order-123")


@pytest.mark.asyncio
async def test_cancel_schedule_with_prefix():
    """cancel_schedule applies prefix to the schedule name."""
    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await cancel_schedule("order-123", prefix="prod")

    mock_client.delete_schedule.assert_called_once_with(Name="prod-order-123")


@pytest.mark.asyncio
async def test_cancel_schedule_noop_when_not_found():
    """cancel_schedule is a no-op when the schedule does not exist."""
    mock_client = MagicMock()

    class ResourceNotFoundException(Exception):  # noqa: N818
        pass

    mock_client.exceptions.ResourceNotFoundException = ResourceNotFoundException
    mock_client.delete_schedule.side_effect = ResourceNotFoundException("not found")

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await cancel_schedule("order-123")  # should not raise


# ── schedule_message: at / cron ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_message_with_at():
    """schedule_message uses at() expression when at= is provided."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    fire_at = datetime(2026, 6, 1, 14, 0, 0, tzinfo=ZoneInfo("America/Mexico_City"))

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="s1",
            phone="+521234567890",
            message="Hi",
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            at=fire_at,
        )

    expr = mock_client.create_schedule.call_args.kwargs["ScheduleExpression"]
    assert expr.startswith("at(")
    assert "2026-06-01T20:00:00" in expr  # Mexico City UTC-6 → 20:00 UTC


@pytest.mark.asyncio
async def test_schedule_message_with_cron_is_recurring():
    """schedule_message with cron= sets ActionAfterCompletion=NONE."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="daily-1",
            phone="+521234567890",
            message="Good morning",
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            cron="0 9 * * ? *",
        )

    kwargs = mock_client.create_schedule.call_args.kwargs
    assert kwargs["ScheduleExpression"] == "cron(0 9 * * ? *)"
    assert kwargs["ActionAfterCompletion"] == "NONE"


@pytest.mark.asyncio
async def test_schedule_message_cron_wraps_bare_expression():
    """Bare cron string without cron() wrapper is auto-wrapped."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="s1",
            phone="+521234567890",
            message="Hi",
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            cron="0 9 * * ? *",
        )

    expr = mock_client.create_schedule.call_args.kwargs["ScheduleExpression"]
    assert expr == "cron(0 9 * * ? *)"


@pytest.mark.asyncio
async def test_schedule_message_cron_precedence_over_at():
    """cron takes precedence over at when both are provided."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_message(
            schedule_id="s1",
            phone="+521234567890",
            message="Hi",
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
            cron="0 9 * * ? *",
        )

    expr = mock_client.create_schedule.call_args.kwargs["ScheduleExpression"]
    assert expr.startswith("cron(")


# ── schedule_operation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_operation_basic():
    """schedule_operation creates a schedule with source=socialagent payload."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        sid = await schedule_operation(
            operation="reminder",
            phone="+521234567890",
            data={"message": "Hello"},
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            delay_minutes=30,
        )

    assert sid is not None
    kwargs = mock_client.create_schedule.call_args.kwargs
    payload = json.loads(kwargs["Target"]["Input"])
    assert payload["source"] == "socialagent"
    assert payload["operation"] == "reminder"
    assert payload["data"]["phone"] == "+521234567890"
    assert payload["data"]["message"] == "Hello"
    assert kwargs["ActionAfterCompletion"] == "DELETE"


@pytest.mark.asyncio
async def test_schedule_operation_explicit_id():
    """Explicit schedule_id is used as-is."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        sid = await schedule_operation(
            operation="reminder",
            phone="+521234567890",
            data={},
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            schedule_id="my-explicit-id",
        )

    assert sid == "my-explicit-id"
    assert mock_client.create_schedule.call_args.kwargs["Name"] == "my-explicit-id"


def _clock_at(*moments: datetime) -> MagicMock:
    """A stand-in for the module's ``datetime`` whose ``now`` returns
    ``moments`` in turn, so a test does not run across a minute boundary."""
    clock = MagicMock(wraps=datetime)
    clock.now.side_effect = [m.astimezone(UTC) for m in moments]
    return clock


@pytest.mark.asyncio
async def test_schedule_operation_auto_id_is_deterministic():
    """Auto-generated schedule_id is deterministic for same inputs, including
    a repeat a few seconds later (a retried call)."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    clock = _clock_at(
        datetime(2026, 10, 10, 10, 0, 5, tzinfo=UTC),
        datetime(2026, 10, 10, 10, 0, 40, tzinfo=UTC),
    )

    with (
        patch(
            "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
        ),
        patch("socialagent.scheduler.eventbridge.datetime", clock),
    ):
        sid1 = await schedule_operation(
            operation="reminder",
            phone="+521234567890",
            data={"x": 1},
            target_arn="arn:",
            role_arn="arn:",
        )
        sid2 = await schedule_operation(
            operation="reminder",
            phone="+521234567890",
            data={"x": 1},
            target_arn="arn:",
            role_arn="arn:",
        )

    assert sid1 == sid2


@pytest.mark.asyncio
async def test_schedule_operation_rerun_later_updates_the_same_schedule():
    """A handler that runs again with the same data, such as an event
    delivered twice a few minutes apart, updates the schedule it made.

    The delay comes from project code, not from a model asked anew, so it is
    hashed as given. Hashing the minute it fires in made the redelivery a
    second schedule, and the user got the follow-up twice.
    """
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    clock = _clock_at(
        datetime(2026, 10, 10, 10, 0, 5, tzinfo=UTC),
        datetime(2026, 10, 10, 10, 3, 5, tzinfo=UTC),
    )
    call = {
        "operation": "order_followup",
        "phone": "+521234567890",
        "data": {"order_id": "ORD-001"},
        "target_arn": "arn:",
        "role_arn": "arn:",
        "delay_minutes": 24 * 60,
    }

    with (
        patch(
            "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
        ),
        patch("socialagent.scheduler.eventbridge.datetime", clock),
    ):
        sid1 = await schedule_operation(**call)
        sid2 = await schedule_operation(**call)

    assert sid1 == sid2
    calls = mock_client.create_schedule.call_args_list
    assert calls[0].kwargs["Name"] == calls[1].kwargs["Name"]
    # The second run moves the one schedule; it does not add another.
    assert [c.kwargs["ScheduleExpression"] for c in calls] == [
        "at(2026-10-11T10:00:05)",
        "at(2026-10-11T10:03:05)",
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            {"at": datetime(2026, 10, 10, 14, 0, tzinfo=UTC)},
            {"at": datetime(2026, 10, 10, 23, 0, tzinfo=UTC)},
            id="two-at-times",
        ),
        pytest.param({"delay_minutes": 10}, {"delay_minutes": 120}, id="two-delays"),
        pytest.param(
            {"cron": "0 15 ? * MON *"}, {"cron": "0 15 ? * THU *"}, id="two-crons"
        ),
        pytest.param({"cron": "0 15 ? * MON *"}, {"delay_minutes": 2}, id="cron-at"),
    ],
)
@pytest.mark.asyncio
async def test_schedule_operation_auto_id_differs_by_timing(first, second):
    """The same data at two times is two schedules, not one overwritten.

    A create on an existing name updates it, so an id built from the data
    alone let a second reminder with the same text replace the first.
    """
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    common = {
        "operation": "reminder",
        "phone": "+521234567890",
        "data": {"message": "Tomar pastilla"},
        "target_arn": "arn:",
        "role_arn": "arn:",
    }

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        sid1 = await schedule_operation(**common, **first)
        sid2 = await schedule_operation(**common, **second)

    assert sid1 != sid2
    for sid in (sid1, sid2):
        assert re.fullmatch(r"521234567890-reminder-[0-9a-f]{8}", sid)
    names = [c.kwargs["Name"] for c in mock_client.create_schedule.call_args_list]
    assert names[0] != names[1]


@pytest.mark.asyncio
async def test_schedule_operation_cron_is_recurring():
    """schedule_operation with cron= sets ActionAfterCompletion=NONE."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_operation(
            operation="daily_checkin",
            phone="+521234567890",
            data={},
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            cron="0 9 * * ? *",
        )

    kwargs = mock_client.create_schedule.call_args.kwargs
    assert kwargs["ScheduleExpression"] == "cron(0 9 * * ? *)"
    assert kwargs["ActionAfterCompletion"] == "NONE"


@pytest.mark.asyncio
async def test_schedule_operation_with_at():
    """schedule_operation uses at() expression when at= is provided."""
    mock_client = MagicMock()
    mock_client.exceptions.ConflictException = Exception
    fire_at = datetime(2026, 6, 1, 20, 0, 0, tzinfo=UTC)

    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_operation(
            operation="reminder",
            phone="+521234567890",
            data={},
            target_arn="arn:aws:lambda:::notify",
            role_arn="arn:aws:iam:::role",
            at=fire_at,
        )

    expr = mock_client.create_schedule.call_args.kwargs["ScheduleExpression"]
    assert expr == "at(2026-06-01T20:00:00)"


@pytest.mark.asyncio
async def test_schedule_operation_phone_wins_over_data() -> None:
    """Explicit phone param should override any phone in data dict."""
    mock_client = MagicMock()
    mock_client.create_schedule.return_value = {}
    with patch(
        "socialagent.scheduler.eventbridge.boto3.client", return_value=mock_client
    ):
        await schedule_operation(
            operation="reminder",
            phone="+521111111111",
            data={"phone": "+525555550199", "msg": "hi"},
            target_arn="arn:aws:lambda:us-east-1:123:function:notif",
            role_arn="arn:aws:iam::123:role/sched",
            delay_minutes=5,
            prefix="test",
        )
    payload = json.loads(
        mock_client.create_schedule.call_args.kwargs["Target"]["Input"]
    )
    assert payload["data"]["phone"] == "+521111111111"
