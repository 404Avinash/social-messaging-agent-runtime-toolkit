# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for notification handler and send_and_log helper.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from socialagent.handlers.common import send_and_log
from socialagent.handlers.notification import make_notification_handler
from socialagent.types import OutboundMessage


def _make_deps():
    provider = MagicMock()
    provider.send_text = AsyncMock(return_value="msg-1")
    provider.send_message = AsyncMock(return_value="msg-2")
    provider.send_template = AsyncMock(return_value="msg-3")
    memory = MagicMock()
    memory.log_assistant_message = AsyncMock()
    return provider, memory


# ── send_and_log ──────────────────────────────────────────────────────────────


def test_send_and_log_text() -> None:
    provider, memory = _make_deps()
    asyncio.run(send_and_log(provider, memory, "+521", "Hola", "Hola"))
    provider.send_text.assert_called_once_with("+521", "Hola")
    memory.log_assistant_message.assert_called_once_with("+521", "Hola")


def test_send_and_log_outbound_message() -> None:
    provider, memory = _make_deps()
    msg = OutboundMessage(type="interactive", payload={})
    asyncio.run(send_and_log(provider, memory, "+521", msg, "rich text"))
    provider.send_message.assert_called_once_with("+521", msg)
    memory.log_assistant_message.assert_called_once_with("+521", "rich text")


def test_send_and_log_swallows_memory_failure_after_delivery() -> None:
    """A memory-write failure must not propagate — the message already went out.

    The send is committed once it returns; letting the log error escape re-runs
    the handler (notification Lambda retries async, scheduler wraps in retry) and
    re-delivers a message the user already got.
    """
    provider, memory = _make_deps()
    memory.log_assistant_message = AsyncMock(
        side_effect=Exception("ThrottlingException")
    )

    # Must not raise.
    asyncio.run(send_and_log(provider, memory, "+521", "Hola", "Hola"))

    provider.send_text.assert_called_once_with("+521", "Hola")


# ── notification handler ──────────────────────────────────────────────────────


def test_send_text_operation() -> None:
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    result = handler(
        {
            "operation": "send_text",
            "data": {"phone": "+521", "text": "Hi", "memory_text": "Hi"},
        }
    )
    assert result["statusCode"] == 200
    provider.send_text.assert_called_once_with("+521", "Hi")
    memory.log_assistant_message.assert_called_once_with("+521", "Hi")


def test_send_template_operation_no_registry() -> None:
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory, registry=None)
    result = handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521",
                "template_name": "welcome",
                "params": ["Ana"],
                "log_to_memory": True,
            },
        }
    )
    assert result["statusCode"] == 200
    provider.send_template.assert_called_once()
    call_args = memory.log_assistant_message.call_args[0]
    assert "welcome" in call_args[1]


def test_send_template_operation_with_registry() -> None:
    provider, memory = _make_deps()
    registry = MagicMock()
    registry.render = MagicMock(return_value="Bienvenida Ana.")
    handler = make_notification_handler(provider, memory, registry=registry)
    handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521",
                "template_name": "welcome",
                "params": ["Ana"],
                "log_to_memory": True,
            },
        }
    )
    registry.render.assert_called_once_with("welcome", ["Ana"])
    memory.log_assistant_message.assert_called_once_with("+521", "Bienvenida Ana.")


def test_send_template_operation_swallows_memory_failure_after_delivery() -> None:
    """A memory-write failure after the send must not propagate.

    The notification Lambda is invoked async, so Lambda would retry the handler
    and send the template again.
    """
    provider, memory = _make_deps()
    memory.log_assistant_message = AsyncMock(
        side_effect=Exception("ThrottlingException")
    )
    handler = make_notification_handler(provider, memory, registry=None)
    result = handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521",
                "template_name": "welcome",
                "params": ["Ana"],
                "log_to_memory": True,
            },
        }
    )
    assert result["statusCode"] == 200
    provider.send_template.assert_called_once()


def test_send_message_operation() -> None:
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    result = handler(
        {
            "operation": "send_message",
            "data": {
                "phone": "+521",
                "type": "interactive",
                "payload": {},
                "memory_text": "Pick one",
            },
        }
    )
    assert result["statusCode"] == 200
    provider.send_message.assert_called_once()
    memory.log_assistant_message.assert_called_once_with("+521", "Pick one")


def test_unknown_operation_returns_400() -> None:
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    result = handler({"operation": "explode", "data": {}})
    assert result["statusCode"] == 400


def test_a_dropped_schedule_is_logged_as_an_error(caplog) -> None:
    """A stack without the scheduler Lambda targets this one with every
    schedule. EventBridge counts the 400 as delivered, so the log line is the
    only trace of the lost reminder: it must reach the error views and say
    what to change."""
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    with caplog.at_level("WARNING", logger="socialagent.handlers.notification"):
        result = handler(
            {
                "source": "socialagent",
                "operation": "reminder",
                "data": {"phone": "+521", "message": "Tomar agua"},
                "schedule_id": "521-reminder-abc",
            }
        )
    assert result["statusCode"] == 400
    provider.send_text.assert_not_called()
    [record] = caplog.records
    assert record.levelname == "ERROR"
    assert "reminder" in record.getMessage()
    assert "scheduler_lambda" in record.getMessage()


def test_a_dropped_schedule_says_an_older_schedule_must_be_recreated(caplog) -> None:
    """Enabling the scheduler Lambda does not move a schedule created while it
    was off: EventBridge fixed its target at creation, so it keeps landing here
    after the redeploy. The line must say so, or it repeats advice the
    developer has already followed."""
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    with caplog.at_level("WARNING", logger="socialagent.handlers.notification"):
        handler(
            {
                "source": "socialagent",
                "operation": "reminder",
                "data": {"phone": "+521", "message": "Tomar agua"},
                "schedule_id": "521-reminder-abc",
            }
        )
    [record] = caplog.records
    assert "recreate" in record.getMessage()


def test_a_dropped_scheduled_message_is_named_as_a_message(caplog) -> None:
    """A schedule_message() payload has no operation. The line must not name
    an empty operation, or call the message a handler."""
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    with caplog.at_level("WARNING", logger="socialagent.handlers.notification"):
        result = handler(
            {
                "source": "socialagent_scheduler",
                "phone": "+521",
                "message": {"type": "text", "text": "Tomar agua"},
                "schedule_id": "pill-8am",
            }
        )
    assert result["statusCode"] == 400
    provider.send_text.assert_not_called()
    [record] = caplog.records
    assert record.levelname == "ERROR"
    assert record.getMessage().startswith("Scheduled message dropped:")
    assert "''" not in record.getMessage()
    assert "schedule_message()" in record.getMessage()
    assert "scheduler_lambda" in record.getMessage()


def test_an_unknown_direct_operation_stays_a_warning(caplog) -> None:
    provider, memory = _make_deps()
    handler = make_notification_handler(provider, memory)
    with caplog.at_level("WARNING", logger="socialagent.handlers.notification"):
        handler({"source": "socialagent", "operation": "explode", "data": {}})
    [record] = caplog.records
    assert record.levelname == "WARNING"
