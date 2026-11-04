# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SMART runtime configuration. Supports explicit construction or env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from socialagent.constants import (
    OUTBOUND_GAP_SECONDS,
    TRANSCRIBE_POLL_INTERVAL,
    TRANSCRIBE_TIMEOUT,
)


@dataclass
class SocialAgentConfig:
    """All configuration for a SMART deployment.

    Required fields must be provided explicitly or via :meth:`from_env`.
    Fields prefixed with ``_`` are infrastructure plumbing auto-discovered
    from environment variables set by Terraform — users never set these directly.
    """

    # Required
    agent_runtime_arn: str
    memory_id: str
    provider: str  # "eum_social" | "meta_cloud" | "twilio"
    phone_number_id: str

    # Optional — user-configurable
    region: str = "us-east-1"
    template_prefix: str = ""
    templates_path: str = ""
    output_channel: str = "whatsapp"
    meta_api_version: str = "v20.0"
    error_messages: dict = field(default_factory=dict)
    log_level: str = "INFO"
    meta_phone_number_id: str = (
        ""  # numeric Meta phone id; filters SNS in shared-WABA stacks
    )

    # Message buffering (inbound debounce) — user-configurable
    message_buffering_enabled: bool = False

    # Inbound attachments: stage images and documents in S3 and list them in the
    # payload so the agent can receive the original (socialagent_runtime.attachments)
    inbound_attachments_enabled: bool = False
    attachments_max_bytes: int = 5 * 1024 * 1024
    buffer_window_seconds: int = 5
    buffer_max_wait_seconds: int = 45
    buffer_ttl_seconds: int = 900

    # Tunable limits
    transcribe_poll_interval: int = TRANSCRIBE_POLL_INTERVAL
    transcribe_timeout: int = TRANSCRIBE_TIMEOUT
    # Transcribe language codes a voice note may be in; () lets Transcribe
    # identify among every language, which misreads a one-word note.
    transcribe_languages: tuple[str, ...] = ()
    outbound_gap_seconds: float = OUTBOUND_GAP_SECONDS

    # Auto-discovered from env (set by Terraform)
    _notification_lambda_arn: str = ""
    _scheduler_lambda_arn: str = ""  # empty when the stack has no scheduler Lambda
    _scheduler_role_arn: str = ""
    _scheduler_prefix: str = ""
    _state_table_name: str = ""
    _buffer_queue_url: str = ""
    _conversation_logs_bucket: str = ""
    _transcription_bucket: str = ""
    _transcription_prefix: str = ""
    _media_prefix: str = ""

    @classmethod
    def from_env(cls) -> SocialAgentConfig:
        """Build config from environment variables.

        Raises :class:`ValueError` listing any missing required variables.
        """
        missing = [
            k for k in ("AGENT_RUNTIME_ARN", "MEMORY_ID") if not os.environ.get(k)
        ]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        return cls(
            agent_runtime_arn=os.environ["AGENT_RUNTIME_ARN"],
            memory_id=os.environ["MEMORY_ID"],
            provider=os.environ.get("SOCIALAGENT_PROVIDER", "eum_social"),
            phone_number_id=os.environ.get("PHONE_NUMBER_ID", ""),
            region=os.environ.get("AWS_REGION", "us-east-1"),
            template_prefix=os.environ.get("TEMPLATE_PREFIX", ""),
            templates_path=os.environ.get("TEMPLATES_PATH", ""),
            output_channel=os.environ.get("OUTPUT_CHANNEL", "whatsapp"),
            meta_api_version=os.environ.get("META_API_VERSION", "v20.0"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            meta_phone_number_id=os.environ.get("SOCIALAGENT_META_PHONE_NUMBER_ID", ""),
            error_messages={
                k: v
                for k, v in {
                    "throttle": os.environ.get("ERROR_MSG_THROTTLE"),
                    "validation": os.environ.get("ERROR_MSG_VALIDATION"),
                    "generic": os.environ.get("ERROR_MSG_GENERIC"),
                    "timeout": os.environ.get("ERROR_MSG_TIMEOUT"),
                }.items()
                if v
            },
            transcribe_poll_interval=int(
                os.environ.get("TRANSCRIBE_POLL_INTERVAL", TRANSCRIBE_POLL_INTERVAL)
            ),
            transcribe_timeout=int(
                os.environ.get("TRANSCRIBE_TIMEOUT", TRANSCRIBE_TIMEOUT)
            ),
            transcribe_languages=tuple(
                code.strip()
                for code in os.environ.get("TRANSCRIBE_LANGUAGES", "").split(",")
                if code.strip()
            ),
            outbound_gap_seconds=float(
                os.environ.get("OUTBOUND_GAP_SECONDS") or OUTBOUND_GAP_SECONDS
            ),
            message_buffering_enabled=os.environ.get(
                "MESSAGE_BUFFERING_ENABLED", "false"
            )
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
            # window is capped at 900s (SQS DelaySeconds maximum); floor of 1s.
            buffer_window_seconds=min(
                900, max(1, int(os.environ.get("BUFFER_WINDOW_SECONDS") or 5))
            ),
            buffer_max_wait_seconds=max(
                1, int(os.environ.get("BUFFER_MAX_WAIT_SECONDS") or 45)
            ),
            buffer_ttl_seconds=max(
                60, int(os.environ.get("BUFFER_TTL_SECONDS") or 900)
            ),
            _notification_lambda_arn=os.environ.get("NOTIFICATION_SERVICE_ARN", ""),
            _scheduler_lambda_arn=os.environ.get("SCHEDULER_LAMBDA_ARN", ""),
            _scheduler_role_arn=os.environ.get("SCHEDULER_ROLE_ARN", ""),
            _scheduler_prefix=os.environ.get("SCHEDULER_PREFIX", ""),
            _state_table_name=os.environ.get("STATE_TABLE_NAME", ""),
            _buffer_queue_url=os.environ.get("BUFFER_QUEUE_URL", ""),
            _conversation_logs_bucket=os.environ.get("CONVERSATION_LOGS_BUCKET", ""),
            _transcription_bucket=os.environ.get("TRANSCRIPTION_BUCKET", ""),
            _transcription_prefix=os.environ.get("TRANSCRIPTION_PREFIX", ""),
            _media_prefix=os.environ.get("MEDIA_PREFIX", ""),
            inbound_attachments_enabled=os.environ.get("INBOUND_ATTACHMENTS", "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
            attachments_max_bytes=int(
                os.environ.get("ATTACHMENTS_MAX_BYTES") or 5 * 1024 * 1024
            ),
        )

    @property
    def media_bucket(self) -> str:
        """The stack's media bucket (the same bucket transcription stages audio in)."""
        return self._transcription_bucket

    @property
    def media_prefix(self) -> str:
        """Key prefix beneath which this stack owns objects on a shared bucket."""
        return self._media_prefix
