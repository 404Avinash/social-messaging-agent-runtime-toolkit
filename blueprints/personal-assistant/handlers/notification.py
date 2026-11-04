# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Notification handler — outbound WhatsApp messages and templates."""

from socialagent import SocialAgentApp, SocialAgentConfig

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

handler = app.notification_handler()
