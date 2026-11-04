# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler handler — EventBridge scheduled events."""

from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.handlers.event import EventContext  # noqa: F401 — used by commented examples below

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

# A handler here runs only on the scheduler Lambda, which the stack deploys
# when socialagent.<env>.yaml sets features.scheduler_lambda: true. Without it
# ctx.schedule and scheduler.schedule refuse to schedule it.
# @app.on_schedule("my_reminder")
# async def handle_reminder(event: dict, ctx: EventContext) -> None:
#     await ctx.send_text(event["phone"], "Reminder!")

handler = app.scheduler_handler()
