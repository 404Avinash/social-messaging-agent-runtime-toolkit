# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inbound message debounce buffer for SMART (Lambda control layer).

OPT-IN, deterministic (timer-only). Coalesces rapid sequential messages from one
user into a single agent invocation so the agent replies once per burst instead
of once per fragment. Runs in the webhook Lambda, BEFORE the runtime is invoked.

Configuration flows through :class:`socialagent.config.SocialAgentConfig` (``from_env()``) — no
direct environment-variable reads. Construct a :class:`MessageBuffer` from the
handler's config and gate it with :meth:`MessageBuffer.is_enabled`.

Public API:
- ``MessageBuffer(config)``              — construct from SocialAgentConfig
- ``buffer.is_enabled()``                — feature flag (off in dev mode)
- ``buffer.ingest_text(phone, mid, text, now)`` / ``buffer.ingest(...)`` — buffer a message
- ``buffer.should_flush(phone, seq, now)`` — decide if a delayed flush should run
- ``buffer.collect_messages(phone)`` / ``buffer.clear(phone)`` — buffer read / drain
- ``combine_prompt(messages)``           — concatenate buffered texts (no config)
"""

from __future__ import annotations

from .core import (
    FlushDecision,
    IngestResult,
    MessageBuffer,
    combine_prompt,
)
from .queue import send_flush
from .store import BufferStore

__all__ = [
    "BufferStore",
    "FlushDecision",
    "IngestResult",
    "MessageBuffer",
    "combine_prompt",
    "send_flush",
]
