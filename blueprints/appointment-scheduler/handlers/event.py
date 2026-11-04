# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Event handler — external events via API Gateway."""

import logging
import os

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.handlers.event import EventContext
from socialagent.utils.phone import normalize_phone

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)


@app.on_event("visit_cancelled")
async def handle_visit_cancelled(event: dict, ctx: EventContext) -> None:
    """Handle business-side cancellation — clean state and notify user."""

    # The agent keys the booking and the reminder on the number as the webhook
    # normalized it, so a caller's +521... or missing "+" must match that form.
    phone = normalize_phone(event["phone"])
    # Read both keys for one release; the template says "see the {{1}}".
    listing = event.get("property") or event.get("car") or "property"
    reason = event.get("reason", "unforeseen circumstances")

    booking = await ctx.state.get(phone, "booking")
    if booking:
        await ctx.state.delete(phone, "booking")
        # A cancellation question the agent asked about this booking is moot now,
        # and so is the record of when its reminder was delivered.
        await ctx.state.delete(phone, "cancel_pending")
        await ctx.state.delete(phone, "reminder_sent")
        digits = "".join(c for c in phone if c.isdigit())
        await ctx.cancel_schedule(f"reminder-{digits}")
        await ctx.write_to_memory(
            phone,
            f"[System: viewing of {listing} was cancelled by the business."
            f" Reason: {reason}. Booking has been removed.]",
        )

    await ctx.send_template(
        phone,
        "visit_cancelled_by_business",
        params=[listing, reason],
        language="en",
    )


handler = app.event_handler()
