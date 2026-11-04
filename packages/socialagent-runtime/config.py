# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Centralized configuration — single source of truth for all env vars.

All modules in socialagent_runtime read from ``config`` instead of calling
``os.environ.get`` directly. Env var names are defined here ONCE.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

try:
    from .constants import DEV_MODE_VALUE
except ImportError:
    from constants import DEV_MODE_VALUE  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    notification_lambda_arn: str
    scheduler_lambda_arn: str
    scheduler_role_arn: str
    scheduler_prefix: str
    state_table_name: str
    media_bucket: str
    region: str
    guardrail_id: str
    guardrail_version: str
    # Whether the stack has the scheduler Lambda, the only one that runs
    # @app.on_schedule handlers. scheduler_lambda_arn cannot tell: it falls back
    # to the notification Lambda when SCHEDULER_LAMBDA_ARN is empty. True by
    # default, so a config built by hand targets the ARN it names, as before.
    has_scheduler_lambda: bool = True

    @property
    def is_dev_mode(self) -> bool:
        # Dev mode is an explicit opt-in: only the sentinel enables it. Treating an
        # *empty* ARN as dev mode meant a production container that lost the
        # variable silently switched onto the dev stubs — notify writing to /tmp and
        # returning success, upload_media returning "dev-media-id" — so every
        # outbound message was dropped while the tools reported success. Absent
        # configuration is a broken deployment, not a request for stubs; from_env
        # now refuses to build such a config at all.
        #
        # Keyed off the notification Lambda ARN only. Other subsystems decide
        # independently (state.py keys off STATE_TABLE_NAME == DEV_MODE_VALUE), so a
        # hand-rolled config can stub notify while state hits real DynamoDB.
        # `socialagent dev` sets both, so a normal local session is fully stubbed.
        return self.notification_lambda_arn == DEV_MODE_VALUE

    @property
    def guardrail_enabled(self) -> bool:
        return bool(self.guardrail_id)

    @property
    def guardrail_config(self) -> dict | None:
        """Return guardrail config dict for use with Bedrock Converse API or Strands."""
        if not self.guardrail_id:
            return None
        return {
            "guardrailIdentifier": self.guardrail_id,
            "guardrailVersion": self.guardrail_version or "DRAFT",
        }

    @classmethod
    def from_env(cls) -> "AgentConfig":
        scheduler_env = os.environ.get("SCHEDULER_LAMBDA_ARN")
        dev = os.environ.get("NOTIFICATION_LAMBDA_ARN", "") == DEV_MODE_VALUE
        cfg = cls(
            notification_lambda_arn=os.environ.get("NOTIFICATION_LAMBDA_ARN", ""),
            # Terraform sets SCHEDULER_LAMBDA_ARN to "" (present but empty) when
            # enable_scheduler_lambda is false, so `os.environ.get(key, fallback)`
            # would return the empty string instead of the fallback. Fall back to
            # the notification Lambda whenever the scheduler ARN is empty OR absent,
            # otherwise EventBridge Scheduler rejects the empty Target.Arn.
            scheduler_lambda_arn=(
                os.environ.get("SCHEDULER_LAMBDA_ARN")
                or os.environ.get("NOTIFICATION_LAMBDA_ARN", "")
            ),
            scheduler_role_arn=os.environ.get("SCHEDULER_ROLE_ARN", ""),
            scheduler_prefix=os.environ.get("SCHEDULER_PREFIX", ""),
            state_table_name=os.environ.get("STATE_TABLE_NAME", ""),
            media_bucket=os.environ.get(
                "MEDIA_BUCKET", os.environ.get("CONVERSATION_LOGS_BUCKET", "")
            ),
            region=os.environ.get("AWS_REGION", "us-east-1"),
            guardrail_id=os.environ.get("BEDROCK_GUARDRAIL_ID", ""),
            guardrail_version=os.environ.get("BEDROCK_GUARDRAIL_VERSION", ""),
            # Terraform always sets the variable, empty without the Lambda, so
            # absent counts as no Lambda when deployed. Under dev mode absent
            # means nobody said: `socialagent dev` sets it from the YAML's
            # features.scheduler_lambda, and a test or a harness of the
            # project's own that leaves it out keeps every operation.
            has_scheduler_lambda=(
                True if dev and scheduler_env is None else bool(scheduler_env)
            ),
        )
        if not cfg.is_dev_mode:
            if not cfg.notification_lambda_arn:
                # Fatal, unlike the warnings below: with no notification Lambda
                # there is no outbound path at all, so the agent could only fail
                # silently on every turn. Better to refuse to start than to accept
                # messages and drop them.
                raise ValueError(
                    "NOTIFICATION_LAMBDA_ARN is not set. The agent cannot send "
                    "anything without it, so it will not start. Set it to the "
                    "stack's notification Lambda ARN, or to the literal "
                    f"{DEV_MODE_VALUE!r} to run with local dev stubs "
                    "(this is what `socialagent dev` does)."
                )
            missing = [
                name
                for name, val in [
                    ("NOTIFICATION_LAMBDA_ARN", cfg.notification_lambda_arn),
                    ("SCHEDULER_PREFIX", cfg.scheduler_prefix),
                    ("SCHEDULER_ROLE_ARN", cfg.scheduler_role_arn),
                    ("MEDIA_BUCKET", cfg.media_bucket),
                ]
                if not val
            ]
            if missing:
                logger.warning(
                    "Missing env vars (tools may fail): %s", ", ".join(missing)
                )
            if not cfg.state_table_name:
                # Deliberately separate from the list above: this one silently
                # loses data instead of failing — state.put "succeeds" to an
                # in-container file that does not survive the next invocation.
                logger.warning(
                    "STATE_TABLE_NAME not set — state falls back to ephemeral "
                    "in-container file storage and WILL NOT persist across turns. "
                    "Set enable_state_table = true if the agent relies on state."
                )
        else:
            logger.info(
                "Running in dev mode (NOTIFICATION_LAMBDA_ARN not set or __socialagent_dev__)"
            )
        return cfg


config = AgentConfig.from_env()
