# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SocialAgentApp: main orchestrator that wires config, provider, memory, and handlers together.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from socialagent.config import SocialAgentConfig
from socialagent.handlers.event import make_event_handler
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduler_event import make_scheduler_handler
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers import MessageProvider
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.state.table import SocialAgentState
from socialagent.templates.registry import TemplateRegistry


def _make_provider(config: SocialAgentConfig) -> MessageProvider:
    """Instantiate the correct MessageProvider from config.

    Raises :class:`ValueError` for unknown provider names.
    """
    if config.provider == "eum_social":
        return EUMSocialProvider(
            phone_number_id=config.phone_number_id,
            region=config.region,
            meta_api_version=config.meta_api_version,
            template_prefix=config.template_prefix,
            transcription_bucket=config._transcription_bucket,
            conversation_logs_bucket=config._conversation_logs_bucket,
            media_prefix=config._media_prefix,
            message_gap_seconds=config.outbound_gap_seconds,
        )
    raise ValueError(f"Unknown provider: {config.provider!r}")


class SocialAgentApp:
    """Central application object for a SMART deployment.

    Instantiate once at module level, register hooks via decorators, then
    export the handler functions returned by the ``*_handler()`` methods as
    Lambda entry points.
    """

    def __init__(self, config: SocialAgentConfig) -> None:
        self.config = config
        self.provider: MessageProvider = _make_provider(config)
        self.memory_writer = SocialAgentMemoryWriter(config.memory_id, config.region)
        self.state = SocialAgentState(config)

        # Set the root log level. In AWS Lambda the runtime installs a root
        # handler before any user code runs, so logging.basicConfig() is a
        # no-op there — setting the level directly is what actually takes
        # effect. LOG_LEVEL is provided per-Lambda via Terraform env vars and
        # surfaced on SocialAgentConfig.
        level = config.log_level.upper()
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))

        self._before_invoke: Callable | None = None
        self._after_invoke: Callable | None = None
        self._on_media: Callable | None = None
        self._on_status: Callable | None = None
        self._event_handlers: dict[str, Callable] = {}
        self._schedule_handlers: dict[str, Callable] = {}

    # ------------------------------------------------------------------ #
    # Hook decorators                                                      #
    # ------------------------------------------------------------------ #

    def before_invoke(self, fn: Callable) -> Callable:
        """Register an async hook called before AgentCore Runtime invocation.

        Signature: ``async (message: InboundMessage, payload: dict) -> dict``
        """
        self._before_invoke = fn
        return fn

    def after_invoke(self, fn: Callable) -> Callable:
        """Register an async hook called after AgentCore Runtime invocation.

        Signature: ``async (message: InboundMessage, response: str) -> str | OutboundMessage``
        """
        self._after_invoke = fn
        return fn

    def on_media(self, fn: Callable) -> Callable:
        """Register an async hook for custom media-to-text conversion.

        Signature: ``async (message: InboundMessage) -> str``
        """
        self._on_media = fn
        return fn

    def on_status(self, fn: Callable) -> Callable:
        """Register an async hook for delivery/read status updates.

        Signature: ``async (status: StatusUpdate) -> None``
        """
        self._on_status = fn
        return fn

    def on_event(self, event_type: str) -> Callable:
        """Register a handler for an external event type (e.g. ``"order_created"``).

        Signature: ``async (event: dict, ctx: EventContext) -> None``
        """

        def decorator(fn: Callable) -> Callable:
            self._event_handlers[event_type] = fn
            return fn

        return decorator

    def on_schedule(self, operation: str) -> Callable:
        """Register a handler for a scheduled operation (e.g. ``"buyer_timeout"``).

        Signature: ``async (event: dict, ctx: EventContext) -> None``
        """

        def decorator(fn: Callable) -> Callable:
            self._schedule_handlers[operation] = fn
            return fn

        return decorator

    # ------------------------------------------------------------------ #
    # Handler factories                                                    #
    # ------------------------------------------------------------------ #

    def webhook_handler(self) -> Callable:
        """Return the Lambda handler for inbound WhatsApp messages via SNS."""
        return make_webhook_handler(
            config=self.config,
            provider=self.provider,
            before_invoke=self._before_invoke,
            after_invoke=self._after_invoke,
            on_media=self._on_media,
            on_status=self._on_status,
        )

    def notification_handler(self) -> Callable:
        """Return the Lambda handler for outbound notifications."""
        registry = (
            TemplateRegistry(self.config.templates_path)
            if self.config.templates_path
            else None
        )
        return make_notification_handler(
            provider=self.provider,
            memory_writer=self.memory_writer,
            registry=registry,
            state=self.state,
            config=self.config,
        )

    def event_handler(self) -> Callable:
        """Return the Lambda handler for external event ingestion."""
        registry = (
            TemplateRegistry(self.config.templates_path)
            if self.config.templates_path
            else None
        )
        return make_event_handler(
            provider=self.provider,
            memory_writer=self.memory_writer,
            event_handlers=self._event_handlers,
            registry=registry,
            state=self.state,
            config=self.config,
        )

    def scheduler_handler(self) -> Callable:
        """Return the Lambda handler for EventBridge scheduled events."""
        registry = (
            TemplateRegistry(self.config.templates_path)
            if self.config.templates_path
            else None
        )
        return make_scheduler_handler(
            provider=self.provider,
            memory_writer=self.memory_writer,
            schedule_handlers=self._schedule_handlers,
            registry=registry,
            state=self.state,
            config=self.config,
        )
