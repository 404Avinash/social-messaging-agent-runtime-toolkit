# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Event handler — external events via API Gateway."""

from socialagent import SocialAgentApp, SocialAgentConfig

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

# @app.on_event("my_event")
# async def handle_my_event(event: dict, ctx: EventContext) -> None:
#     await ctx.send_text(event["phone"], "Event received!")

handler = app.event_handler()
