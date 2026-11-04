# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
EventBridge Scheduler integration for SMART one-shot scheduled messages.
"""

from __future__ import annotations

import hashlib
import asyncio
import contextlib
import json
import re
from datetime import UTC, datetime, timedelta

import boto3

from socialagent.constants import (
    SCHEDULED_TIME_ATTRIBUTE,
    SCHEDULER_ACTION_AFTER_COMPLETION,
    SCHEDULER_FLEXIBLE_WINDOW,
)
from socialagent.types import OutboundMessage


def _safe_name(value: str) -> str:
    """Replace non-alphanumeric characters with hyphens for schedule name safety."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", value).lower()


def _schedule_name(prefix: str, schedule_id: str) -> str:
    """Build a deterministic EventBridge schedule name from prefix and ID.

    Format: ``{prefix}-{safe_id}`` (or just ``{safe_id}`` when prefix is empty).
    Truncated to 64 chars (AWS limit) with a 7-char hash suffix to preserve uniqueness.
    """
    safe_id = _safe_name(schedule_id)
    name = f"{prefix}-{safe_id}" if prefix else safe_id
    if len(name) > 64:
        digest = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
        name = name[:56] + "-" + digest[:7]
    return name


def _at_expression(delay_minutes: int) -> str:
    """Return an EventBridge ``at(...)`` expression for ``delay_minutes`` from now.

    Args:
        delay_minutes: Number of minutes from now to fire the schedule.
    """
    fire_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
    return f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"


def _message_to_payload(message: str | OutboundMessage) -> dict:
    """Serialize a message to the EventBridge target input payload dict."""
    if isinstance(message, OutboundMessage):
        return {"type": message.type, "payload": message.payload}
    return {"type": "text", "text": message}


def operation_schedule_id(phone: str, operation: str, data: dict, timing: str) -> str:
    """The id :func:`schedule_operation` generates when given none.

    ``{digits}-{operation}-{hash}``, the hash taken over ``data`` and ``timing``
    (the expression as written, or ``delay(<minutes>)``). ``socialagent dev``
    returns it from a handler's ``ctx.schedule`` too, which it does not create.
    """
    material = json.dumps([data, timing], sort_keys=True)
    h = hashlib.sha256(material.encode()).hexdigest()[:8]
    digits = re.sub(r"\D", "", phone)
    return f"{digits}-{operation}-{h}"


def _create_schedule(
    name: str,
    expression: str,
    target_arn: str,
    role_arn: str,
    input_payload: dict,
    region: str,
    *,
    recurring: bool = False,
) -> None:
    """Create or update (upsert) an EventBridge Scheduler schedule.

    Existing schedules with the same name are overwritten — this is intentional
    to make scheduling idempotent. One-shot schedules auto-delete after firing;
    recurring schedules persist until explicitly cancelled.
    """
    client = boto3.client("scheduler", region_name=region)
    kwargs = {
        "Name": name,
        "ScheduleExpression": expression,
        "FlexibleTimeWindow": {"Mode": SCHEDULER_FLEXIBLE_WINDOW},
        "Target": {
            "Arn": target_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(input_payload),
        },
        "ActionAfterCompletion": "NONE"
        if recurring
        else SCHEDULER_ACTION_AFTER_COMPLETION,
    }
    try:
        client.create_schedule(**kwargs)
    except client.exceptions.ConflictException:
        client.update_schedule(**kwargs)


def _delete_schedule(name: str, region: str) -> None:
    """Delete an EventBridge schedule by name. No-op if it does not exist."""
    client = boto3.client("scheduler", region_name=region)
    with contextlib.suppress(client.exceptions.ResourceNotFoundException):
        client.delete_schedule(Name=name)


async def schedule_message(
    schedule_id: str,
    phone: str,
    message: str | OutboundMessage,
    target_arn: str,
    role_arn: str,
    delay_minutes: int = 0,
    at: datetime | None = None,
    *,
    cron: str | None = None,
    prefix: str = "",
    region: str = "us-east-1",
) -> str:
    """Schedule a message via EventBridge Scheduler.

    One-shot (default): auto-deletes after firing.
    Recurring: pass ``cron`` — schedule persists until cancelled via :func:`cancel_schedule`.

    Timing precedence: ``cron`` > ``at`` > ``delay_minutes``.

    Args:
        schedule_id: Caller-supplied ID for upsert/cancel semantics.
        phone: E.164 recipient phone number.
        message: Plain text or :class:`OutboundMessage` to send.
        target_arn: ARN of the scheduler Lambda, the only one that delivers
            this payload; the notification Lambda drops it.
        role_arn: IAM role ARN that EventBridge Scheduler uses to invoke the target.
        delay_minutes: Minutes from now to fire (ignored if ``at`` or ``cron`` set).
        at: Timezone-aware datetime for when to fire (ignored if ``cron`` set).
        cron: EventBridge cron expression — makes the schedule recurring.
        prefix: Optional schedule name prefix (e.g. ``"prod-myapp"``).
        region: AWS region.

    Returns:
        The schedule ID (pass to :func:`cancel_schedule` to cancel).
    """
    name = _schedule_name(prefix, schedule_id)
    if cron is not None:
        expression = cron if cron.startswith("cron(") else f"cron({cron})"
    elif at is not None:
        fire_at = at.astimezone(UTC) if at.tzinfo else at
        expression = f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"
    else:
        expression = _at_expression(delay_minutes)
    input_payload = {
        "source": "socialagent_scheduler",
        "phone": phone,
        "message": _message_to_payload(message),
        "schedule_id": schedule_id,
        # EventBridge fills in when this firing was due, the same on every
        # delivery of it, so the scheduler Lambda runs a firing at most once.
        "scheduled_time": SCHEDULED_TIME_ATTRIBUTE,
    }
    await asyncio.to_thread(
        _create_schedule,
        name,
        expression,
        target_arn,
        role_arn,
        input_payload,
        region,
        recurring=cron is not None,
    )
    return schedule_id


async def cancel_schedule(
    schedule_id: str,
    prefix: str = "",
    region: str = "us-east-1",
) -> None:
    """Cancel a pending schedule by its ID.

    No-op if the schedule does not exist (already fired or never created).

    Args:
        schedule_id: The same ID passed to :func:`schedule_message`.
        prefix: Must match the prefix used when scheduling.
        region: AWS region.
    """
    name = _schedule_name(prefix, schedule_id)
    await asyncio.to_thread(_delete_schedule, name, region)


async def schedule_operation(
    operation: str,
    phone: str,
    data: dict,
    target_arn: str,
    role_arn: str,
    delay_minutes: int = 0,
    at: datetime | None = None,
    *,
    cron: str | None = None,
    prefix: str = "",
    region: str = "us-east-1",
    schedule_id: str | None = None,
) -> str:
    """Schedule an operation via EventBridge Scheduler (operation routing path).

    Builds a ``source: socialagent`` payload routed by the scheduler Lambda to
    ``@app.on_schedule`` handlers.

    One-shot (default): auto-deletes after firing.
    Recurring: pass ``cron`` — schedule persists until cancelled via :func:`cancel_schedule`.

    Timing precedence: ``cron`` > ``at`` > ``delay_minutes``.

    An omitted ``schedule_id`` is generated from the phone, operation, data and
    timing: a ``cron`` or ``at`` as written, a delay as ``delay_minutes``. The
    same data at two different times makes two schedules rather than the
    second replacing the first, and a handler that runs again with the same
    data and delay updates the schedule it made.

    Returns the schedule ID used.
    """
    if cron is not None:
        expression = cron if cron.startswith("cron(") else f"cron({cron})"
    elif at is not None:
        fire_at = at.astimezone(UTC) if at.tzinfo else at
        expression = f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"
    else:
        expression = _at_expression(delay_minutes)
    if not schedule_id:
        # A delay is hashed as given, not as the time it fires: it comes from
        # project code, so a handler that runs again with the same data (an
        # event delivered twice, minutes apart) updates the schedule it made
        # instead of sending the follow-up twice. The agent-side scheduler
        # hashes the minute a delay fires in instead, as the model asks anew.
        timing = (
            expression
            if cron is not None or at is not None
            else f"delay({delay_minutes})"
        )
        schedule_id = operation_schedule_id(phone, operation, data, timing)
    name = _schedule_name(prefix, schedule_id)
    input_payload = {
        "source": "socialagent",
        "operation": operation,
        "data": {**data, "phone": phone},
        "schedule_id": schedule_id,
        # As in schedule_message; a built-in send on a stack without the
        # scheduler Lambda goes to the notification Lambda, which records it too.
        "scheduled_time": SCHEDULED_TIME_ATTRIBUTE,
    }
    await asyncio.to_thread(
        _create_schedule,
        name,
        expression,
        target_arn,
        role_arn,
        input_payload,
        region,
        recurring=cron is not None,
    )
    return schedule_id
