# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQS enqueue helper for the message-buffer delayed flush (control layer).

The webhook schedules a flush by sending a message to the buffer SQS queue with
``DelaySeconds`` equal to the debounce window. After the delay, SQS delivers the
message back to the webhook (its second event source), which decides — via the
generation token — whether this flush should actually run.

No-op when no queue is configured (dev mode / buffering disabled), so callers may
invoke it unconditionally.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache

import boto3

from socialagent.config import SocialAgentConfig

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _sqs_client(region: str):
    return boto3.client("sqs", region_name=region)


async def send_flush(config: SocialAgentConfig, phone: str, seq: int) -> None:
    """Enqueue a delayed flush for ``phone`` carrying generation token ``seq``.

    ``DelaySeconds`` is the debounce window — the flush fires that many seconds
    later (SQS caps DelaySeconds at 900). No-op when the queue URL is unset, so
    the caller doesn't need to special-case dev mode.
    """
    queue_url = config._buffer_queue_url
    if not queue_url:
        return
    delay = min(900, max(0, config.buffer_window_seconds))
    body = json.dumps({"phone": phone, "seq": seq})
    await asyncio.to_thread(
        _sqs_client(config.region).send_message,
        QueueUrl=queue_url,
        MessageBody=body,
        DelaySeconds=delay,
    )
