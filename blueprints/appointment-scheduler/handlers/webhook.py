# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Webhook handler — inbound WhatsApp messages via SNS."""

import logging
import os

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from socialagent import SocialAgentApp, SocialAgentConfig

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

handler = app.webhook_handler()
