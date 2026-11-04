# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The log lines checked here name a user by the last four digits of the phone,
and carry no text.

CloudWatch Logs data protection masks phone numbers only for the countries in
``pii_phone_number_countries`` (it has no identifier for Mexico) and only in the
Lambda log groups, not the agent runtime's, and it masks only the identifiers it
recognises, not message text as such. So
the code itself must not log a full number or a user's words, at any level:
``LOG_LEVEL`` is a per-Lambda setting an operator can turn up to DEBUG.

Two gaps against that rule are still open, under M6 in docs/threat-model.md,
and nothing here checks them. A WhatsApp message id (wamid) encodes the user's
number, and the attachment warnings and the personal-assistant webhook's PDF
warning log it whole. An exception's own text can quote the number, and a
traceback, or a warning that quotes the error such as the agent runtime's
session_read_recovered, logs it unmasked. The message ids in these tests encode
no number, and the exceptions that reach a traceback or an unmasked warning quote
none; the errors here that do quote the number are raised on lines that mask the
error text.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.buffer import FlushDecision, IngestResult, MessageBuffer, store
from socialagent.config import SocialAgentConfig
from socialagent.handlers.common import log_delivered, send_and_log
from socialagent.handlers.event import make_event_handler
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.providers.utils import extract_changes
from socialagent.types import MessageType
from socialagent.utils.phone import mask_phone

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import notify as runtime_notify  # noqa: E402
import scheduler as runtime_scheduler  # noqa: E402
from bedrock_agentcore.memory.integrations.strands import (  # noqa: E402
    session_manager as session_manager_mod,
)

import socialagent_runtime  # noqa: E402
from socialagent_runtime.phone import mask_phone as runtime_mask_phone  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

PHONE = "+525555550100"
# The number with or without its country code: neither may reach a log line.
DIGITS = "5555550100"
MESSAGE_TEXT = "Take the blue pill"


def _assert_masked(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "nothing was logged, so nothing was checked"
    assert DIGITS not in caplog.text
    assert "blue pill" not in caplog.text


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        outbound_gap_seconds=0,
        **kwargs,
    )


def _inbound(text: str = MESSAGE_TEXT) -> MagicMock:
    return MagicMock(id="wamid.1", sender=PHONE, type=MessageType.TEXT, text=text)


def _provider() -> MagicMock:
    p = MagicMock()
    p.parse_webhook = AsyncMock(return_value=_inbound())
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    p.send_template = AsyncMock()
    return p


# ── the provider: every outbound send ─────────────────────────────────────────


def test_provider_send_log_masks_the_recipient(caplog):
    p = EUMSocialProvider("phone-id")
    p._client = MagicMock()
    p._client.send_whatsapp_message.return_value = {"messageId": "wamid.out"}
    with caplog.at_level(logging.DEBUG):
        asyncio.run(p.send_text(PHONE, MESSAGE_TEXT))
    assert "send message_id=wamid.out to=+...0100 type=text" in caplog.text
    _assert_masked(caplog)


# ── the webhook: a turn, its queued messages, and every way it can fail ───────


def test_a_turn_and_its_queued_messages_log_no_full_number(caplog):
    queued = [
        {"operation": "send_text", "text": MESSAGE_TEXT, "phone": PHONE},
        {"operation": "send_template", "template_name": "t", "params": [MESSAGE_TEXT]},
    ]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=(MESSAGE_TEXT, queued),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "Outbound[0] op=send_text type=send_text to +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_queued_only_reply_logs_no_full_number(caplog):
    queued = [{"operation": "send_text", "text": MESSAGE_TEXT}]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime", return_value=(None, queued)
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no text for +...0100" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    "error",
    ["ThrottlingException", "ValidationException: bad input", "boom"],
    ids=["throttled", "validation", "generic"],
)
def test_a_failed_turn_logs_no_full_number(caplog, error):
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=RuntimeError(error),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_validation_error_that_quotes_the_session_is_logged_masked(caplog):
    """The error text is logged too, and it can quote the session id, which
    carries the number."""
    error = "ValidationException: no session whatsapp-session-conversation-525555550100"
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=RuntimeError(error),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no session whatsapp-session-conversation-...0100" in caplog.text
    _assert_masked(caplog)


def test_a_reply_with_no_usable_text_logs_no_full_number(caplog):
    with (
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=(None, [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no usable reply text for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_failed_prompt_resolution_logs_no_full_number(caplog):
    async def before_invoke(_message, _payload):
        raise RuntimeError("hook failed")

    with caplog.at_level(logging.DEBUG):
        make_webhook_handler(_config(), _provider(), before_invoke=before_invoke)(
            {"e": 1}
        )
    assert "before_invoke failed for +...0100" in caplog.text
    _assert_masked(caplog)


def test_an_empty_media_prompt_logs_no_full_number(caplog):
    provider = _provider()
    provider.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender=PHONE, type=MessageType.IMAGE, text=None, raw={}
        )
    )

    async def on_media(_message):
        return "  "

    with (
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), provider, on_media=on_media)({"e": 1})
    assert "image message from +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_rate_limited_user_is_logged_by_the_last_four_digits(caplog):
    with (
        patch("socialagent.handlers.webhook.is_rate_limited", return_value=True),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    # The first eight characters were logged before: country and area code.
    assert "Rate limited user +...0100" in caplog.text
    _assert_masked(caplog)


def _buffering(fake: MagicMock):
    return patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake)


def _fake_buffer(flush: FlushDecision | None = None) -> MagicMock:
    buf = MagicMock()
    buf.is_enabled = MagicMock(return_value=True)
    buf.ingest_text = AsyncMock(return_value=IngestResult(seq=3, flush_now=False))
    buf.should_flush = AsyncMock(return_value=flush)
    buf.clear_flushed = AsyncMock()
    return buf


def test_buffered_text_log_masks_the_sender(caplog):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    with (
        _buffering(_fake_buffer()),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())({"e": 1})
    assert "buffered text from +...0100 (seq=3" in caplog.text
    _assert_masked(caplog)


def _sqs_flush() -> dict:
    body = json.dumps({"phone": PHONE, "seq": 3})
    return {"Records": [{"eventSource": "aws:sqs", "body": body}]}


@pytest.mark.parametrize("should_flush", [True, False], ids=["flushed", "skipped"])
def test_a_buffer_flush_logs_no_full_number(caplog, should_flush):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    messages = [{"type": "text", "text": MESSAGE_TEXT, "message_id": "wamid.1"}]
    decision = FlushDecision(should_flush, "window_elapsed", messages)
    with (
        _buffering(_fake_buffer(flush=decision)),
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())(_sqs_flush())
    assert "+...0100" in caplog.text
    _assert_masked(caplog)


def test_a_failed_buffer_flush_logs_no_full_number(caplog):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    messages = [{"type": "text", "text": MESSAGE_TEXT, "message_id": "wamid.1"}]
    decision = FlushDecision(True, "window_elapsed", messages)

    async def before_invoke(_message, _payload):
        raise RuntimeError("hook failed")

    with (
        _buffering(_fake_buffer(flush=decision)),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider(), before_invoke=before_invoke)(
            _sqs_flush()
        )
    assert "before_invoke failed for +...0100" in caplog.text
    _assert_masked(caplog)


def test_buffer_ingest_logs_the_last_four_digits(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        store, "_DEV_PATH", tmp_path / "socialagent-dev-test" / "b.json"
    )
    buffer = MessageBuffer(_config(message_buffering_enabled=True))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(buffer.ingest_text(PHONE, "wamid.1", MESSAGE_TEXT, 1000.0))
    assert "buffer.ingest phone=+...0100" in caplog.text
    _assert_masked(caplog)


# ── out-of-band sends, the event audit line and the memory writer ────────────


def test_send_and_log_and_a_failed_memory_write_log_no_full_number(caplog):
    provider = _provider()
    writer = MagicMock()
    writer.log_assistant_message = AsyncMock(side_effect=RuntimeError("memory down"))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(send_and_log(provider, writer, PHONE, MESSAGE_TEXT, MESSAGE_TEXT))
        asyncio.run(log_delivered(writer, PHONE, MESSAGE_TEXT))
    assert "Sent and logged message to +...0100" in caplog.text
    assert "Message delivered to +...0100 but memory write failed" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    ("phone", "logged"),
    [
        (PHONE, "+...0100"),
        # An external system may format the number, or send it as a JSON number.
        ("+52 55 5555 0100", "+...0100"),
        (525555550100, "...0100"),
    ],
    ids=["e164", "formatted", "number"],
)
def test_the_event_audit_line_masks_the_phone(caplog, phone, logged):
    async def on_event(_event, _ctx):
        return None

    handler = make_event_handler(MagicMock(), MagicMock(), {"order": on_event})
    with caplog.at_level(logging.DEBUG):
        handler({"event_type": "order", "phone": phone, "data": {"note": MESSAGE_TEXT}})
    (line,) = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert json.loads(line.split("SOCIALAGENT_EVENT ", 1)[1])["phone"] == logged
    _assert_masked(caplog)


def test_the_memory_writer_logs_masked_ids(caplog):
    writer = SocialAgentMemoryWriter(memory_id="mem-x")
    writer._client = MagicMock()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(writer.log_assistant_message(PHONE, MESSAGE_TEXT))
    writer._client.create_event.assert_called_once()
    assert "...0100 / whatsapp-session-conversation-...0100" in caplog.text
    _assert_masked(caplog)


def test_an_unparseable_entry_logs_its_keys_not_its_content(caplog):
    entry = {
        "contacts": [{"wa_id": PHONE.lstrip("+"), "profile": {"name": "Ana"}}],
        "messages": [{"from": PHONE.lstrip("+"), "text": {"body": MESSAGE_TEXT}}],
    }
    with caplog.at_level(logging.DEBUG):
        assert extract_changes(entry) is None
    assert "keys=['contacts', 'messages']" in caplog.text
    assert "Ana" not in caplog.text
    _assert_masked(caplog)


# ── the personal-assistant blueprint's handler Lambdas ────────────────────────


def _blueprint_scheduler_module(monkeypatch):
    """The personal-assistant handlers/scheduler.py, imported as the Lambda does."""
    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
        "OUTBOUND_GAP_SECONDS": "0",
    }.items():
        monkeypatch.setenv(k, v)
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    handlers_dir = REPO / "blueprints" / "personal-assistant" / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_pa_sched_probe", handlers_dir / "scheduler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)
    return mod


def _blueprint_scheduler(monkeypatch):
    """The personal-assistant reminder handler."""
    return _blueprint_scheduler_module(monkeypatch).app._schedule_handlers["reminder"]


@pytest.mark.parametrize("template_fails", [False, True], ids=["template", "text"])
def test_the_reminder_handler_logs_neither_the_number_nor_the_text(
    monkeypatch, caplog, template_fails
):
    handler = _blueprint_scheduler(monkeypatch)
    ctx = MagicMock()
    # The provider's errors quote the recipient, as a send failure can.
    ctx.send_template = AsyncMock(
        side_effect=RuntimeError(f"no template for {PHONE}") if template_fails else None
    )
    ctx.send_text = AsyncMock(side_effect=RuntimeError(f"no text for {PHONE}"))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(handler({"phone": PHONE, "message": MESSAGE_TEXT}, ctx))
    assert f"Reminder handler: phone=+...0100 chars={len(MESSAGE_TEXT)}" in caplog.text
    if template_fails:
        assert "no template for +...0100" in caplog.text
        assert "no text for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_task_result_that_cannot_follow_its_notice_logs_no_full_number(
    monkeypatch, caplog
):
    """A result the preview cannot carry follows as text. Here that send and the
    memory write both fail, with errors that quote the number."""
    mod = _blueprint_scheduler_module(monkeypatch)
    monkeypatch.setattr(
        mod,
        "call_runtime",
        AsyncMock(return_value={"result": f"{MESSAGE_TEXT}\nevery morning"}),
    )
    ctx = MagicMock()
    ctx.lambda_context = None
    ctx.send_template = AsyncMock()
    ctx.send_text = AsyncMock(side_effect=RuntimeError(f"no text for {PHONE}"))
    ctx.write_to_memory = AsyncMock(
        side_effect=RuntimeError(f"memory refused actor {DIGITS}")
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            mod.handle_invoke_agent(
                {"phone": PHONE, "prompt": "remind me", "schedule_id": "s-1"}, ctx
            )
        )
    ctx.send_text.assert_awaited_once()
    ctx.write_to_memory.assert_awaited_once()
    assert "not delivered: no text for +...0100" in caplog.text
    assert "memory refused actor ...0100" in caplog.text
    _assert_masked(caplog)


# ── the agent runtime, whose log group has no data protection policy at all ──


def _mask_phone_source(path: Path) -> str:
    """The source of ``mask_phone`` and the patterns it uses, from one copy."""
    source = path.read_text()
    names = {"mask_phone", "_PHONE_SHAPED", "_DIGIT_RUN"}
    return "\n".join(
        ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if getattr(node, "name", None) in names
        or any(getattr(t, "id", None) in names for t in getattr(node, "targets", []))
    )


def test_the_runtime_masks_with_the_same_code_as_the_lambdas() -> None:
    """The layers never import each other, so each keeps a copy; one fix, both copies."""
    sdk = _mask_phone_source(REPO / "src" / "socialagent" / "utils" / "phone.py")
    runtime = _mask_phone_source(REPO / "packages" / "socialagent-runtime" / "phone.py")
    assert "def mask_phone" in sdk
    assert runtime == sdk, (
        "mask_phone in packages/socialagent-runtime/phone.py differs from the one in "
        "src/socialagent/utils/phone.py; copy one over the other"
    )


@pytest.mark.parametrize(
    "value",
    [
        PHONE,
        "+52 55 5555 0100",
        "whatsapp-session-conversation-525555550100",
        "sa-dev-525555550100-reminder-ab12",
        "1234",
        "",
        None,
    ],
)
def test_the_runtime_masks_like_the_lambdas(value) -> None:
    assert runtime_mask_phone(value) == mask_phone(value)


def _guarded_manager(monkeypatch, read_session, read_agent):
    """A session manager with the runtime's corrupt-event guard installed on it."""

    class FakeAgentCoreMemorySessionManager:
        pass

    FakeAgentCoreMemorySessionManager.read_session = read_session
    FakeAgentCoreMemorySessionManager.read_agent = read_agent
    monkeypatch.setattr(
        session_manager_mod,
        "AgentCoreMemorySessionManager",
        FakeAgentCoreMemorySessionManager,
    )
    monkeypatch.setattr(socialagent_runtime, "_session_read_guard_installed", False)
    socialagent_runtime._install_session_read_guard()
    manager = FakeAgentCoreMemorySessionManager()
    manager.memory_client = SimpleNamespace(list_events=lambda **kw: [])
    manager.config = SimpleNamespace(
        actor_id=PHONE.lstrip("+"), memory_id="mem-1", session_id="s"
    )
    return manager


@pytest.mark.parametrize("recovers", [True, False], ids=["restored", "fresh"])
def test_a_recovered_session_read_logs_masked_ids(monkeypatch, caplog, recovers):
    calls = []

    def read_session(self, session_id, **kwargs):
        calls.append(session_id)
        if len(calls) == 1:
            raise IndexError("list index out of range")
        return {"session": 1} if recovers else None

    manager = _guarded_manager(monkeypatch, read_session, lambda *a, **k: None)
    with caplog.at_level(logging.DEBUG):
        manager.read_session("whatsapp-session-conversation-525555550100")
    assert (
        "session_id=whatsapp-session-conversation-...0100 actor_id=...0100"
        in caplog.text
    )
    _assert_masked(caplog)


def test_a_recovered_agent_read_logs_masked_ids(monkeypatch, caplog):
    calls = []

    def read_agent(self, session_id, agent_id, **kwargs):
        calls.append(agent_id)
        return {"agent": 1} if len(calls) > 1 else None

    manager = _guarded_manager(monkeypatch, lambda *a, **k: None, read_agent)
    with caplog.at_level(logging.DEBUG):
        manager.read_agent("whatsapp-session-conversation-525555550100", "default")
    assert (
        "session_id=whatsapp-session-conversation-...0100 agent_id=default "
        "actor_id=...0100" in caplog.text
    )
    _assert_masked(caplog)


class _ProdSchedulerConfig:
    is_dev_mode = False
    scheduler_prefix = ""
    region = "us-east-1"


def test_a_refused_cancel_logs_the_schedule_name_masked(monkeypatch, caplog):
    """The name embeds its owner's digits, and the owner is another user."""
    monkeypatch.setattr(runtime_scheduler, "config", _ProdSchedulerConfig())
    monkeypatch.setattr(runtime_scheduler, "_scheduler_client", MagicMock())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            runtime_scheduler.cancel("525555550100-reminder-ab12", phone="+15555550199")
        )
    assert "schedule '...0100-reminder-ab12' is not owned" in caplog.text
    _assert_masked(caplog)


def test_a_schedule_whose_detail_fails_is_logged_masked(monkeypatch, caplog):
    client = MagicMock()
    client.list_schedules.return_value = {
        "Schedules": [{"Name": "525555550100-reminder-ab12"}]
    }
    client.get_schedule.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(runtime_scheduler, "config", _ProdSchedulerConfig())
    monkeypatch.setattr(runtime_scheduler, "_scheduler_client", lambda: client)
    with caplog.at_level(logging.DEBUG):
        results = asyncio.run(runtime_scheduler.list_schedules(PHONE))
    assert results == [
        {"name": "525555550100-reminder-ab12", "state": "UNKNOWN", "expression": ""}
    ]
    assert "could not fetch detail for ...0100-reminder-ab12" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    "send",
    [
        lambda: runtime_notify.send_text(PHONE, MESSAGE_TEXT, immediate=True),
        lambda: runtime_notify.send_template(
            PHONE, "t", [MESSAGE_TEXT], immediate=True
        ),
        lambda: runtime_notify.send_message(
            PHONE, {"type": "text", "text": {"body": MESSAGE_TEXT}}, immediate=True
        ),
    ],
    ids=["text", "template", "message"],
)
def test_a_failed_immediate_send_logs_no_full_number(monkeypatch, caplog, send):
    client = MagicMock()
    client.invoke.side_effect = RuntimeError("lambda down")
    monkeypatch.setattr(runtime_notify, "_lambda_client", lambda: client)
    object.__setattr__(
        runtime_notify.config, "notification_lambda_arn", "arn:aws:lambda:::function:n"
    )
    try:
        with caplog.at_level(logging.DEBUG):
            asyncio.run(send())
    finally:
        object.__setattr__(
            runtime_notify.config, "notification_lambda_arn", "__socialagent_dev__"
        )
    assert "failed for +...0100" in caplog.text
    _assert_masked(caplog)


# ── the webhook's runtime-call, buffering and reaction paths ──────────────────


def test_a_runtime_timeout_logs_no_full_number(caplog):
    from botocore.exceptions import ReadTimeoutError

    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=ReadTimeoutError(endpoint_url="https://bedrock-agentcore"),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "did not answer in time for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_retried_runtime_call_logs_no_full_number(caplog):
    from botocore.exceptions import ClientError

    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeAgentRuntime",
    )
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=[throttled, ("ok", [])],
        ),
        patch("socialagent.handlers.runtime_call.asyncio.sleep", new=AsyncMock()),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "AgentCore call for +...0100 did not start" in caplog.text
    _assert_masked(caplog)


def test_a_text_that_cannot_be_buffered_logs_no_full_number(caplog):
    """The buffer's DynamoDB key is ``buf#{phone}``, so an error that quotes the
    item it failed on carries the full number. Only its type is logged."""
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    with (
        _buffering(_fake_buffer()),
        patch(
            "socialagent.handlers.webhook.send_flush",
            new=AsyncMock(side_effect=RuntimeError(f"no flush for buf#{PHONE}")),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())({"e": 1})
    assert "Buffering failed for +...0100 (RuntimeError)" in caplog.text
    _assert_masked(caplog)


def test_a_removed_reaction_logs_no_full_number(caplog):
    provider = _provider()
    provider.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.r",
            sender=PHONE,
            type=MessageType.REACTION,
            reaction=SimpleNamespace(emoji="", reacted_message_id="wamid.1"),
        )
    )
    with caplog.at_level(logging.DEBUG):
        make_webhook_handler(_config(), provider)({"e": 1})
    assert "Reaction removed by +...0100" in caplog.text
    _assert_masked(caplog)
