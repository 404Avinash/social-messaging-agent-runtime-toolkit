# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler handler — EventBridge scheduled events."""

import asyncio
import functools
import logging
import os
import time
from collections.abc import Awaitable, Callable

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from botocore.exceptions import ClientError

from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.handlers.common import gap_seconds, text_chunks
from socialagent.handlers.event import EventContext
from socialagent.messages import location
from socialagent.types import OutboundMessage

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)
logger = logging.getLogger(__name__)

# How long the delivery marker (the `reminder_sent` state key) outlives the
# reminder. It only has to outlast a cancel question that was open when the
# reminder arrived, which the tool keeps live for 30 minutes.
REMINDER_SENT_TTL_MINUTES = 180

# Lambda does not retry this function (a retry would resend what a run had
# already sent), so a send that fails is tried again here. Only a throttled
# send, which WhatsApp never received: the AWS SDK has already retried it, and
# reminders due at the same minute fire together. Any other error is raised.
_SEND_ATTEMPTS = 3
_SEND_PAUSE_SECONDS = 2  # times the attempt number
_THROTTLE_CODES = frozenset({"ThrottledRequestException", "ThrottlingException"})


async def _send_with_retry(send: Callable[[], Awaitable[None]]) -> None:
    for attempt in range(1, _SEND_ATTEMPTS + 1):
        try:
            await send()
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in _THROTTLE_CODES or attempt == _SEND_ATTEMPTS:
                raise
            logger.warning(
                "Scheduled send throttled (attempt %d/%d), trying again",
                attempt,
                _SEND_ATTEMPTS,
            )
            await asyncio.sleep(_SEND_PAUSE_SECONDS * attempt)


class _ThrottleRetryContext:
    """The handler's EventContext, with send_template and send_message tried
    again when throttled. Each send is tried on its own, so a throttled pin
    does not send the template again. send_text is left as it is: tried again
    as a whole it would resend the chunks of a long text already sent, and sent
    chunk by chunk, as send_message does, it would no longer write the text to
    memory once."""

    def __init__(self, ctx: EventContext) -> None:
        self._ctx = ctx

    def __getattr__(self, name: str):
        return getattr(self._ctx, name)

    async def send_template(self, *args, **kwargs) -> None:
        await _send_with_retry(lambda: self._ctx.send_template(*args, **kwargs))

    async def send_message(
        self, phone: str, message: OutboundMessage, memory_text: str = ""
    ) -> None:
        """A text too long for one WhatsApp message goes out one chunk at a
        time, each chunk tried on its own: the provider chunks it inside one
        send, so trying that send again would resend the chunks it had already
        delivered. Each chunk then reaches memory as its own message, in place
        of ``memory_text``, since EventContext has no send that leaves memory
        out.
        """
        chunks: list[str] = []
        if message.type == "text":
            chunks = text_chunks(message.payload.get("text", {}).get("body", ""))
        if len(chunks) < 2:
            await _send_with_retry(
                lambda: self._ctx.send_message(phone, message, memory_text=memory_text)
            )
            return
        for i, chunk in enumerate(chunks):
            if i:
                # Paced as EventContext.send_text paces its chunks: WhatsApp can
                # deliver messages sent back to back out of order, and the pause
                # is dropped near the Lambda's timeout so the rest still goes out.
                # An older `socialagent dev` stub has no lambda_context.
                await asyncio.sleep(
                    gap_seconds(
                        config.outbound_gap_seconds,
                        getattr(self._ctx, "lambda_context", None),
                    )
                )
            await _send_with_retry(lambda c=chunk: self._ctx.send_text(phone, c))


def _retry_throttled_sends(handler):
    @functools.wraps(handler)
    async def wrapped(data: dict, ctx: EventContext) -> None:
        await handler(data, _ThrottleRetryContext(ctx))

    return wrapped


@app.on_schedule("visit_reminder")
@_retry_throttled_sends
async def handle_visit_reminder(data: dict, ctx: EventContext) -> None:
    """Send a visit reminder to the user.

    Before the template goes out, the handler closes any open cancel question; the
    moment it is out, before the location pin and the memory note, it records when
    it was delivered, so the cancel tool can tell a reply to the reminder from a
    reply to the card whatever the clock says: EventBridge Scheduler has no delivery
    SLA, so the moment the booking scheduled the reminder for (``reminder_at``) is
    not the moment it arrived.

    A retried or duplicated delivery writes the marker again with a later ``at``.
    The tool compares that moment with when the asking turn began, so the later
    value only closes more questions, never fewer: no delivery token is needed.
    """
    # The booking turn stores the conversation's language with the schedule, so
    # the reminder arrives in it. The registry keys templates by name, so the
    # Spanish text is its own template.
    spanish = str(data.get("language", "en")).startswith("es")
    # A cancel question the agent asked before this reminder is moot once the
    # reminder arrives: the replies a reminder invites ("Sí, gracias", "Yes,
    # thanks", "Yes") are on the cancel tool's closed list of typed yeses, and
    # would cancel the visit the user has just confirmed they will attend. A state
    # failure must not cost the user the reminder: this Lambda is not retried, so
    # the firing would be gone.
    try:
        await ctx.state.delete(data["phone"], "cancel_pending")
    except Exception:
        logger.warning(
            "Visit reminder could not clear the open cancel question", exc_info=True
        )
    await ctx.send_template(
        data["phone"],
        "visit_reminder_es" if spanish else "visit_reminder",
        # "car" is the key reminders scheduled by an earlier version of this
        # blueprint carry; read both for one release so a pending one still fires.
        params=[
            data.get("property") or data.get("car", ""),
            data["time"],
            data["location"],
        ],
        language="es_MX" if spanish else "en",
    )
    # The user has the reminder now. The delete above can land inside the turn that
    # is asking, so the record the tool then writes looks like a question asked
    # after the reminder. The marker says when the reminder was in fact delivered
    # and which booking it was about, in the shape the tool's _booking_key computes
    # ([property, date, time]); a reminder scheduled before `date` travelled in the
    # payload stores None, which the tool reads as the current booking (one reminder
    # per phone, and a rebook drops the marker). It is written right after the send,
    # not before it, so `at` is a delivery and not an attempt that may have failed,
    # and not later: the pin waits ten seconds, and a quick reply to the reminder
    # runs in that gap, when a question asked past the fallback window would still
    # read as live. A later `at` from a duplicate delivery only closes more
    # questions. It is best-effort: the reminder is already out, and an error here
    # would only send a delivered firing to the dead-letter queue. Without the
    # marker (the state table off, or the write failed) the tool falls back to a
    # fixed window after reminder_at, which does not cover a reminder delivered
    # more than that window late.
    booking = (
        [data.get("property") or data.get("car", ""), data["date"], data["time"]]
        if "date" in data
        else None
    )
    try:
        await ctx.state.put(
            data["phone"],
            "reminder_sent",
            {"at": int(time.time()), "booking": booking},
            ttl_minutes=REMINDER_SENT_TTL_MINUTES,
        )
    except Exception:
        logger.warning(
            "Visit reminder delivered but its delivery marker failed", exc_info=True
        )
    # The coordinates come in the payload: this Lambda does not ship src/.
    # Reminders scheduled before they were added carry none, so skip the pin.
    if "lat" in data and "lng" in data:
        await asyncio.sleep(10)
        loc = location(
            data["lat"],
            data["lng"],
            name=data.get("location_name", ""),
            address=data["location"],
        )
        await ctx.send_message(
            data["phone"],
            loc,
            memory_text=f"[Location sent: {data['location']}]",
        )
    # The reminder is already delivered. write_to_memory raises on failure, which
    # would send a delivered reminder's event to the DLQ as a failed one.
    try:
        await ctx.write_to_memory(
            data["phone"],
            f"[System: visit reminder sent for {data.get('property') or data.get('car', '')} at {data['time']}]",
        )
    except Exception:
        logger.exception("Visit reminder delivered but its memory note failed")


handler = app.scheduler_handler()
