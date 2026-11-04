# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Calling AgentCore Runtime from a Lambda handler.

InvokeAgentRuntime runs a whole agent turn, tools and sends included, so it is
not idempotent. A call is sent with botocore's retries off and tried again only
when it failed before AgentCore could run the turn. The wait for the agent fits
the Lambda's remaining time, so the handler can still deliver the answer, or an
error, before the Lambda times out. The webhook calls it, and a schedule
handler that runs an agent turn can too, passing ``ctx.lambda_context``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
)

from socialagent.constants import (
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_MIN_WAIT_SECONDS,
    RUNTIME_READ_TIMEOUT_SECONDS,
    RUNTIME_REPLY_RESERVE_SECONDS,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One attempt only: botocore's default retry of a read timeout would run the
# turn, and its tools, again.
RUNTIME_CLIENT_CONFIG = Config(
    connect_timeout=RUNTIME_CONNECT_TIMEOUT_SECONDS,
    read_timeout=RUNTIME_READ_TIMEOUT_SECONDS,
    retries={"total_max_attempts": 1},
)

# A call AgentCore throttled, or one that never connected, ran no turn, so
# call_runtime sends it again itself: up to this many tries in all, the pause
# before each retry doubling from about this many seconds, with jitter.
RUNTIME_ATTEMPTS = 3
RUNTIME_RETRY_BASE_SECONDS = 1


def seconds_left(context: object) -> float | None:
    """Seconds before the Lambda times out, or None when not running in Lambda."""
    get_remaining = getattr(context, "get_remaining_time_in_millis", None)
    if get_remaining is None:
        return None
    return get_remaining() / 1000


def runtime_read_timeout(context: object) -> float:
    """Seconds to wait for the agent: the cap, or less when the Lambda has less.

    The wait ends ``RUNTIME_REPLY_RESERVE_SECONDS`` before the Lambda does, so a
    turn that starts late (after a long voice-note transcription, say) still
    ends with the timeout error, not with the Lambda killed before anything is
    sent.
    """
    left = seconds_left(context)
    if left is None:
        return RUNTIME_READ_TIMEOUT_SECONDS
    budget = left - RUNTIME_CONNECT_TIMEOUT_SECONDS - RUNTIME_REPLY_RESERVE_SECONDS
    return min(RUNTIME_READ_TIMEOUT_SECONDS, budget)


def turn_never_started(exc: Exception) -> bool:
    """True when a runtime call failed before AgentCore could run the turn.

    A throttled request was refused, and one that never connected was never
    sent, so trying either again cannot run the turn twice. A read timeout or a
    dropped connection can come after the turn started, so neither counts.
    """
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError)):
        return True
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ThrottlingException"
    )


def invoke_runtime(
    runtime_arn: str,
    session_id: str,
    payload: dict,
    region: str,
    read_timeout: float = RUNTIME_READ_TIMEOUT_SECONDS,
) -> object:
    """Invoke AgentCore Runtime once and return its decoded JSON response.

    Passes ``session_id`` as the ``runtimeSessionId`` header so AgentCore
    serializes concurrent invocations of the same session. A reply slower than
    ``read_timeout`` raises ``ReadTimeoutError``; botocore sends the call once.
    """
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=RUNTIME_CLIENT_CONFIG.merge(Config(read_timeout=read_timeout)),
    )
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    raw = resp["response"]
    body = raw.read() if hasattr(raw, "read") else raw
    return json.loads(body)


async def call_runtime(invoke: Callable[[float], T], context: object, label: str) -> T:
    """Run ``invoke(read_timeout)`` in a thread, trying again a call that ran no turn.

    Each try waits for the agent only as long as the Lambda ``context`` has left
    (see :func:`runtime_read_timeout`). A try that never reached the agent is
    made again, up to ``RUNTIME_ATTEMPTS`` in all, unless the pause before it
    would leave under ``RUNTIME_MIN_WAIT_SECONDS`` to wait; then its error is
    raised. Raises ``TimeoutError`` without calling when less than that is left
    to wait: the turn would run, tools included, and almost surely time out.
    ``label`` names the caller in the log.
    """
    attempt = 1
    while True:
        read_timeout = runtime_read_timeout(context)
        if read_timeout < RUNTIME_MIN_WAIT_SECONDS:
            raise TimeoutError("too little time left to wait for the agent")
        try:
            return await asyncio.to_thread(invoke, read_timeout)
        except Exception as exc:
            if attempt == RUNTIME_ATTEMPTS or not turn_never_started(exc):
                raise
            pause = RUNTIME_RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            pause *= random.uniform(0.5, 1)
            if runtime_read_timeout(context) - pause < RUNTIME_MIN_WAIT_SECONDS:
                raise
            logger.warning(
                "AgentCore call for %s did not start (%s), trying again in %.1fs",
                label,
                type(exc).__name__,
                pause,
            )
            await asyncio.sleep(pause)
            attempt += 1
