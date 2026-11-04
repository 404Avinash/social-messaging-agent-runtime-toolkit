# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Writes out-of-band messages (templates, scheduler events) to AgentCore Memory.

Uses boto3 directly to avoid pulling in the heavy bedrock-agentcore SDK
(which brings pydantic, starlette, uvicorn) into the Lambda layer.

"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config

from socialagent.constants import DEV_MODE_VALUE
from socialagent.utils.phone import (
    actor_id_for_phone,
    mask_phone,
    session_id_for_phone,
)

logger = logging.getLogger(__name__)

# Writes after a delivery go through handlers/common.log_delivered, which logs a
# failure rather than re-sending, so this retry is the only one they get;
# EventContext.write_to_memory calls the writer directly and still raises.
# Standard mode retries throttling, HTTP 500/502/503/504, the modeled
# RetryableConflictException, and connection errors and timeouts, with jittered
# exponential backoff. A retry cannot duplicate the event: create_event's
# clientToken is an idempotency token, which botocore fills once per call and
# reuses on every attempt.
#
# Three attempts, not more: the event and scheduler Lambdas time out at 30s, and
# a timeout re-runs the whole invocation, sending the message again. Backoff is
# rand(0, 1) * 2**(n - 1) seconds, so the two retries sleep at most 3s; the old
# default, legacy mode with five attempts, could sleep 15s. Only botocore's
# opt-in AWS_NEW_RETRIES_2026 mode honours x-amz-retry-after, up to 5s more per
# retry. total_max_attempts counts the first call; Config's max_attempts counts
# only the retries.
#
# Each attempt is bounded too, or one hung CreateEvent would take botocore's 60s
# connect and read timeouts and outlast the Lambda. On a dev stack CreateEvent
# took 68 ms at p50, 141 ms at p99 and 456 ms at most (520 calls, 14 days), so
# 2s to read is still four times the tail. A write now gives up within
# 3 x (connect + read) + 3s of backoff = 12s, or 22s when botocore's new retries
# add the 2 x 5s of x-amz-retry-after; either way 5s of the 30s Lambda are left
# for start-up and the send. read_timeout is per socket read, not per response.
_MEMORY_MAX_ATTEMPTS = 3
_MEMORY_CONNECT_TIMEOUT_S = 1
_MEMORY_READ_TIMEOUT_S = 2
_MEMORY_RETRY_CONFIG = Config(
    connect_timeout=_MEMORY_CONNECT_TIMEOUT_S,
    read_timeout=_MEMORY_READ_TIMEOUT_S,
    retries={"mode": "standard", "total_max_attempts": _MEMORY_MAX_ATTEMPTS},
)


def _session_message_payload(text: str) -> list[dict]:
    """Build a SessionMessage-compatible assistant payload for create_event.

    Matches the format written by AgentCoreMemorySessionManager.create_message
    so that out-of-band messages appear correctly in the agent's restored conversation.

    The role is fixed on purpose. The runtime's ``get_turns`` unwraps this JSON
    only in assistant events, because a user can type the same JSON; writing a
    user event in this format would make the two indistinguishable.
    """
    session_msg = {
        "message": {"role": "assistant", "content": [{"text": text}]},
        "message_id": 0,
        "metadata": {},
    }
    return [
        {
            "conversational": {
                "role": "ASSISTANT",
                "content": {"text": json.dumps(session_msg)},
            }
        }
    ]


class SocialAgentMemoryWriter:
    """Writes assistant-role messages to AgentCore Memory on behalf of SMART infrastructure.

    Used by the notification handler and scheduler handler to record outbound
    messages that originate outside the agent loop (templates, reminders, etc.).
    The agent itself is responsible for writing its own conversation turns.

    In dev mode (``memory_id == DEV_MODE_VALUE``), all writes are no-ops.
    """

    def __init__(self, memory_id: str, region: str = "us-east-1") -> None:
        self._memory_id = memory_id
        self._dev = memory_id == DEV_MODE_VALUE
        self._client = (
            None
            if self._dev
            else boto3.client(
                "bedrock-agentcore", region_name=region, config=_MEMORY_RETRY_CONFIG
            )
        )
        # Last eventTimestamp written per session, to keep them strictly
        # increasing — see _monotonic_ts.
        self._last_ts: dict[str, datetime] = {}

    def _monotonic_ts(self, session_id: str) -> datetime:
        """Return a timestamp strictly after this session's previous write.

        botocore serializes eventTimestamp as a whole-second unix timestamp, so
        two writes in the same wall-clock second land on the same value and the
        restore order between them becomes service-determined — the agent could
        see an assistant reply before the system note that set it up. Force at
        least one second of spacing, mirroring the upstream session manager
        (AgentCoreMemorySessionManager._get_monotonic_timestamp).
        """
        now = datetime.now(UTC)
        previous = self._last_ts.get(session_id)
        if previous is not None and now <= previous:
            now = previous + timedelta(seconds=1)
        self._last_ts[session_id] = now
        return now

    async def log_assistant_message(self, phone: str, text: str) -> None:
        """Write an assistant-role message to the memory session for ``phone``.

        No-op in dev mode.

        Args:
            phone: E.164 phone number of the conversation participant.
            text: Rendered message text to persist.
        """
        if self._dev:
            logger.debug("[memory:%s] assistant -> %s", phone, text)
            try:
                import socialagent_runtime as _socialagent_runtime  # noqa: PLC0415

                _socialagent_runtime._dev_turns_for(phone).append((text, "assistant"))
            except ImportError:
                pass
            return
        actor_id = actor_id_for_phone(phone)
        session_id = session_id_for_phone(phone)
        await asyncio.to_thread(
            self._client.create_event,
            memoryId=self._memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=self._monotonic_ts(session_id),
            payload=_session_message_payload(text),
        )
        logger.debug(
            "Memory write: assistant → %s / %s",
            mask_phone(actor_id),
            mask_phone(session_id),
        )
