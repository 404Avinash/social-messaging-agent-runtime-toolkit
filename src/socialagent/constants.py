# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared constants for the SMART SDK.

All magic strings, limits, and default values live here so they can be
imported by any module without creating circular dependencies.
"""

# ── Versioning ────────────────────────────────────────────────────────────────

SOCIALAGENT_VERSION: int = 1
"""Payload contract version between the Lambda side (socialagent) and the agent side (socialagent-runtime).
Both packages must agree on this value; a mismatch raises a clear error at runtime."""

# ── Messaging limits ──────────────────────────────────────────────────────────

MAX_MSG_LEN: int = 1600
"""Maximum WhatsApp message length in characters. Longer text is auto-chunked by send_text."""

OUTBOUND_GAP_SECONDS: float = 3.0
"""Seconds between consecutive messages sent to one user in one go.

WhatsApp does not guarantee delivery order for messages sent back to back: a
button message and the text sent 16 ms after it arrived inverted. The gap
separates queued attachments from each other and from the reply text, and the
chunks of a long text. Override per stack with ``OUTBOUND_GAP_SECONDS``
(Terraform variable ``outbound_gap_seconds``)."""

# ── Provider ──────────────────────────────────────────────────────────────────

MEDIA_TYPES: tuple[str, ...] = ("image", "audio", "video", "document", "sticker")
"""Ordered tuple of Meta webhook media type keys checked during message parsing."""

# ── Default error messages ────────────────────────────────────────────────────

DEFAULT_ERRORS: dict[str, str] = {
    "throttle": "The service is experiencing high demand. Please try again in a moment.",
    "validation": "There was an error processing your request.",
    "generic": "Sorry, I had a problem processing your message.",
    "timeout": "That took longer than expected. Please try again.",
}
"""Default user-facing error messages. Override per-project via ``SocialAgentConfig.error_messages``
or env vars ``ERROR_MSG_THROTTLE``, ``ERROR_MSG_VALIDATION``, ``ERROR_MSG_GENERIC``, ``ERROR_MSG_TIMEOUT``."""

MONTHS_ES: tuple[str, ...] = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
"""Spanish month names, 1-indexed via ``MONTHS_ES[month - 1]``."""

DEV_MODE_VALUE: str = "__socialagent_dev__"
"""Sentinel value for env vars that activates local dev-mode stubs.

When set (e.g. ``STATE_TABLE_NAME=__socialagent_dev__``), the corresponding module
replaces real AWS calls with in-memory or console-print stubs. Set automatically
by ``socialagent dev``; never used in production.
"""

# ── Scheduler ─────────────────────────────────────────────────────────────────

SCHEDULER_FLEXIBLE_WINDOW: str = "OFF"
"""EventBridge Scheduler flexible time window mode — exact timing, no jitter."""

SCHEDULER_ACTION_AFTER_COMPLETION: str = "DELETE"
"""Auto-delete one-shot schedules after they fire."""

SCHEDULER_SOURCE: str = "socialagent_scheduler"
"""Value of the ``source`` field in EventBridge Scheduler payloads from ``schedule_message()``."""

SCHEDULED_TIME_ATTRIBUTE: str = "<aws.scheduler.scheduled-time>"
"""EventBridge Scheduler's context attribute for the time a firing was due.

Every schedule's Input carries it as ``scheduled_time``, and Scheduler replaces
it with the same time on every delivery of one firing and a new time on each
firing of a recurring schedule. So the schedule id and this time name one
firing, which the Lambda the schedule targets records to run it at most once:
the scheduler Lambda, or the notification Lambda for a built-in send on a
stack without one. The execution id cannot serve: Scheduler gives each attempt
a new one. Mirrored in ``packages/socialagent-runtime/constants.py``, which
cannot import this package; a test keeps the two equal.
"""

SCHEDULE_FIRING_TTL_MINUTES: int = 48 * 60
"""How long the record of a firing is kept, by either Lambda that writes one.

Longer than a firing can arrive again: Scheduler retries a delivery Lambda did
not accept for up to 24 hours by default, and Lambda's async queue keeps an
event for up to 6 hours.
"""

NOTIFICATION_SCHEDULE_OPERATIONS: frozenset[str] = frozenset(
    {"send_message", "send_template", "send_text"}
)
"""The operations a schedule can run on a stack without the scheduler Lambda.

There a schedule targets the notification Lambda, which routes only these sends
(``handlers/notification.py``) and answers 400 to anything else. EventBridge
counts the 400 as delivered, so a schedule for any other operation would be
lost, and ``EventContext.schedule`` refuses it instead. ``send_typing`` and
``upload_media`` are routed too but left out: a typing indicator needs the id of
a message that is seconds old, and an upload's media id would reach nobody.
Mirrored in ``packages/socialagent-runtime/constants.py``, which cannot import
this package; a test keeps the two equal.
"""

# ── Runtime invocation ────────────────────────────────────────────────────────

RUNTIME_CONNECT_TIMEOUT_SECONDS: int = 5
"""Seconds the webhook waits to open a connection to AgentCore Runtime."""

RUNTIME_READ_TIMEOUT_SECONDS: int = 100
"""Most seconds the webhook waits for the agent's reply before sending the timeout error.

A slow reply is never asked for again: InvokeAgentRuntime runs a whole agent
turn, so a retry runs the turn and its tools again. Only a call AgentCore
throttled, or one that never connected, is sent again, as it ran no turn
(``runtime_call.call_runtime``). The webhook waits less when the Lambda has less
time left (after a long voice-note transcription, say), so the wait always
ends ``RUNTIME_REPLY_RESERVE_SECONDS`` before the Lambda does.
With nothing spent before the call, connect plus read timeout plus that reserve
is the webhook Lambda's 120 s timeout (``infra-terraform/modules/lambdas/main.tf``)."""

RUNTIME_REPLY_RESERVE_SECONDS: int = 15
"""Seconds of the Lambda's time kept back after the runtime call.

They send the queued messages and the reply, or the timeout error."""

RUNTIME_MIN_WAIT_SECONDS: int = 10
"""Fewest seconds worth waiting for the agent: with less left, no turn is started.

A shorter wait times out on almost any turn while the turn keeps running on the
runtime, tools included, so a reminder it creates is created again when the
user asks again after the timeout error."""

# ── Transcription ─────────────────────────────────────────────────────────────

TRANSCRIBE_POLL_INTERVAL: int = 2
"""Seconds between Amazon Transcribe job status polls."""

TRANSCRIBE_TIMEOUT: int = 90
"""Maximum seconds to wait for a transcription job to complete before raising."""

TRANSCRIBE_S3_PREFIX: str = "socialagent-transcribe/"
"""S3 key prefix for temporary audio files uploaded for transcription."""
