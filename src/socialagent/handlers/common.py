# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared send helpers used by the webhook and the notification, event and
scheduler handlers.

Centralises the pattern: send a message via the provider, then write the
rendered text to AgentCore Memory as an assistant-role event, without letting
a failed write re-send the message. Also splits a long text into the chunks
WhatsApp takes, with gaps that fit the Lambda's time.
"""

from __future__ import annotations

import logging

from socialagent.constants import MAX_MSG_LEN
from socialagent.handlers.runtime_call import seconds_left
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers import MessageProvider
from socialagent.providers.utils import chunk_text
from socialagent.types import OutboundMessage
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)

# Once fewer seconds than this would be left after a gap, the remaining sends
# go out back to back: enough for a handful of sends.
SEND_RESERVE_SECONDS = 10


def gap_seconds(gap: float, context: object) -> float:
    """The pause before the next send: ``gap``, or none near the end.

    Messages sent back to back may arrive out of order, which beats the Lambda
    (``context``) timing out mid-send: the rest would never go out, and a
    redelivered event would run its work again.
    """
    left = seconds_left(context)
    if left is not None and left - gap < SEND_RESERVE_SECONDS:
        return 0
    return gap


def text_chunks(text: str) -> list[str]:
    """``text`` as the WhatsApp messages it goes out in, each sent whole.

    The provider would split a longer text itself and pause the full gap
    between its chunks, whatever time the Lambda has left. The literal two
    characters ``\\n`` are made line breaks first, as the provider does.
    """
    return chunk_text(text.replace(r"\n", "\n"), MAX_MSG_LEN)


async def send_and_log(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    phone: str,
    message: str | OutboundMessage,
    memory_text: str,
) -> None:
    """Send a message and write its rendered text to AgentCore Memory.

    Args:
        provider: Messaging provider used to deliver the message.
        memory_writer: Writer that persists the message to AgentCore Memory.
        phone: E.164 recipient phone number.
        message: The message to send — plain text or an OutboundMessage.
        memory_text: Human-readable text representation written to memory
            (e.g. the rendered template body).
    """
    if isinstance(message, OutboundMessage):
        await provider.send_message(phone, message)
    else:
        await provider.send_text(phone, message)
    await log_delivered(memory_writer, phone, memory_text)
    logger.debug("Sent and logged message to %s", mask_phone(phone))


async def log_delivered(
    memory_writer: SocialAgentMemoryWriter, phone: str, memory_text: str
) -> None:
    """Write a message the provider already delivered to AgentCore Memory.

    Every write that follows a send goes through here, templates and scheduled
    text included.

    Args:
        memory_writer: Writer that persists the message to AgentCore Memory.
        phone: E.164 recipient phone number.
        memory_text: Human-readable text representation written to memory.
    """
    # The send is committed once it returns. The memory write is best-effort:
    # letting it raise here re-runs the whole handler — the notification Lambda is
    # invoked async (InvocationType="Event", up to 2 retries) and a schedule's
    # built-in send is wrapped in with_retry (handlers/scheduled.py) — so a
    # transient AgentCore Memory error would re-deliver a message the user
    # already received. A missing memory log is a smaller harm than a duplicate
    # WhatsApp message.
    try:
        await memory_writer.log_assistant_message(phone, memory_text)
    except Exception:
        logger.exception(
            "Message delivered to %s but memory write failed; not retrying the send",
            mask_phone(phone),
        )
