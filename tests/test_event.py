# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for EventContext and make_event_handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.config import SocialAgentConfig
from socialagent.handlers.event import (
    EventContext,
    SchedulerUnavailableError,
    make_event_handler,
)
from socialagent.types import OutboundMessage


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def provider():
    """Mock MessageProvider."""
    p = AsyncMock()
    p.send_text = AsyncMock(return_value="msg-1")
    p.send_message = AsyncMock(return_value="msg-2")
    p.send_template = AsyncMock(return_value="msg-3")
    return p


@pytest.fixture
def memory_writer():
    """Mock SocialAgentMemoryWriter."""
    w = AsyncMock()
    w.log_assistant_message = AsyncMock()
    return w


@pytest.fixture
def registry():
    """Mock TemplateRegistry."""
    r = MagicMock()
    r.render = MagicMock(return_value="Rendered body")
    return r


@pytest.fixture
def ctx(provider, memory_writer, registry):
    """EventContext with mocked dependencies."""
    return EventContext(provider, memory_writer, registry)


# ── EventContext.send_text ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_text_sends_and_logs(ctx, provider, memory_writer):
    """send_text calls provider.send_text and logs to memory."""
    await ctx.send_text("+521234567890", "Hello")
    provider.send_text.assert_awaited_once_with("+521234567890", "Hello")
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Hello"
    )


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        **kwargs,
    )


class _LambdaClock:
    def __init__(self, seconds_left: float) -> None:
        self.left = seconds_left

    def get_remaining_time_in_millis(self) -> int:
        return int(self.left * 1000)


_LONG_TEXT = "palabra " * 600  # 4800 characters: three WhatsApp messages


@pytest.mark.asyncio
async def test_a_long_text_goes_out_in_chunks_logged_once(
    provider, memory_writer, registry
):
    ctx = EventContext(
        provider,
        memory_writer,
        registry,
        config=_config(outbound_gap_seconds=2),
    )
    ctx.lambda_context = _LambdaClock(100)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with patch("asyncio.sleep", fake_sleep):
        await ctx.send_text("+521234567890", _LONG_TEXT)

    chunks = [c.args[1] for c in provider.send_text.await_args_list]
    assert len(chunks) == 3
    assert "".join(chunks) == _LONG_TEXT
    assert slept == [2, 2]
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", _LONG_TEXT
    )


@pytest.mark.asyncio
async def test_a_long_text_drops_its_gaps_when_the_lambda_is_ending(
    provider, memory_writer, registry
):
    """A scheduled task's result after a slow turn: the provider slept the
    full gap between chunks, and the Lambda timed out with the text cut."""
    ctx = EventContext(
        provider,
        memory_writer,
        registry,
        config=_config(outbound_gap_seconds=10),
    )
    ctx.lambda_context = _LambdaClock(15)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with patch("asyncio.sleep", fake_sleep):
        await ctx.send_text("+521234567890", _LONG_TEXT)

    assert provider.send_text.await_count == 3
    assert slept == [0, 0]


# ── EventContext.send_template ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_template_with_registry(ctx, provider, memory_writer, registry):
    """send_template uses registry.render for memory text."""
    await ctx.send_template("+521234567890", "welcome", ["Alice"], language="es_MX")
    provider.send_template.assert_awaited_once_with(
        "+521234567890", "welcome", "es_MX", ["Alice"]
    )
    registry.render.assert_called_once_with("welcome", ["Alice"])
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Rendered body"
    )


@pytest.mark.asyncio
async def test_send_template_without_registry(provider, memory_writer):
    """send_template falls back to placeholder when no registry."""
    ctx = EventContext(provider, memory_writer, registry=None)
    await ctx.send_template("+521234567890", "welcome", ["Alice"], language="es_MX")
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "[Template: welcome]"
    )


@pytest.mark.asyncio
async def test_send_template_custom_language(ctx, provider):
    """send_template passes language through to provider."""
    await ctx.send_template("+521234567890", "tmpl", [], language="en_US")
    provider.send_template.assert_awaited_once_with(
        "+521234567890", "tmpl", "en_US", []
    )


@pytest.mark.asyncio
async def test_send_template_language_defaults_to_es_mx(ctx, provider):
    """`language` is optional, defaulting to es_MX like notify.send_template.

    It was a required positional, so every documented example that omits it
    (docs/getting-started.md, docs/events-scheduling.md) raised TypeError before
    reaching the send.
    """
    await ctx.send_template("+521234567890", "tmpl", ["Alice"])
    provider.send_template.assert_awaited_once_with(
        "+521234567890", "tmpl", "es_MX", ["Alice"]
    )


@pytest.mark.asyncio
async def test_send_template_swallows_memory_failure_after_delivery(
    ctx, provider, memory_writer
):
    """A memory-write failure after the send must not propagate.

    A failed invocation is retried, and the retry would send the template again.
    """
    memory_writer.log_assistant_message.side_effect = Exception("ThrottlingException")
    await ctx.send_template("+521234567890", "welcome", ["Alice"])
    provider.send_template.assert_awaited_once()


# ── EventContext.send_message ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_with_memory_text(ctx, provider, memory_writer):
    """send_message sends OutboundMessage and logs provided memory_text."""
    msg = OutboundMessage(type="interactive", payload={})
    await ctx.send_message("+521234567890", msg, memory_text="Pick one")
    provider.send_message.assert_awaited_once_with("+521234567890", msg)
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Pick one"
    )


@pytest.mark.asyncio
async def test_send_message_default_memory_text(ctx, memory_writer):
    """send_message uses fallback memory_text when none provided."""
    msg = OutboundMessage(type="image", payload={})
    await ctx.send_message("+521234567890", msg)
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "[image message]"
    )


# ── EventContext.write_to_memory ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_to_memory_assistant(ctx, memory_writer):
    """write_to_memory writes an assistant message."""
    await ctx.write_to_memory("+521234567890", "Some event")
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Some event"
    )


@pytest.mark.asyncio
async def test_write_to_memory_user_role_skipped(ctx, memory_writer):
    """write_to_memory no longer accepts a role param — old test replaced."""
    # role param removed; write_to_memory is assistant-only by design
    await ctx.write_to_memory("+521234567890", "Some text")
    memory_writer.log_assistant_message.assert_awaited_once()


# ── make_event_handler ─────────────────────────────────────────────────────────


def test_handler_routes_to_registered_handler(provider, memory_writer):
    """Handler calls the registered function for a known event_type."""
    called_with = {}

    async def on_order(event: dict, ctx: EventContext) -> None:
        called_with["event"] = event
        called_with["ctx"] = ctx

    handler = make_event_handler(provider, memory_writer, {"order_created": on_order})
    result = handler({"event_type": "order_created", "data": {"id": "123"}})

    assert result["statusCode"] == 200
    assert called_with["event"] == {"id": "123"}
    assert isinstance(called_with["ctx"], EventContext)


def test_handler_returns_400_for_unknown_event_type(provider, memory_writer):
    """Handler returns 400 when no handler is registered for the event_type."""
    handler = make_event_handler(provider, memory_writer, {})
    result = handler({"event_type": "unknown_event", "data": {}})
    assert result["statusCode"] == 400
    assert "unknown_event" in result["body"]


def test_handler_returns_400_for_missing_event_type(provider, memory_writer):
    """Handler returns 400 when event_type field is absent."""
    handler = make_event_handler(provider, memory_writer, {})
    result = handler({"data": {}})
    assert result["statusCode"] == 400


def test_handler_passes_registry_to_context(provider, memory_writer, registry):
    """EventContext created by handler uses the provided registry."""
    captured_ctx = {}

    async def on_event(_event: dict, ctx: EventContext) -> None:
        captured_ctx["ctx"] = ctx

    handler = make_event_handler(
        provider, memory_writer, {"ping": on_event}, registry=registry
    )
    handler({"event_type": "ping", "data": {}})
    assert captured_ctx["ctx"]._registry is registry


def test_handler_no_registry(provider, memory_writer):
    """Handler works without a registry (registry=None)."""

    async def on_event(_event: dict, _ctx: EventContext) -> None:
        pass

    handler = make_event_handler(provider, memory_writer, {"ping": on_event})
    result = handler({"event_type": "ping", "data": {}})
    assert result["statusCode"] == 200


def test_handler_context_is_reused_across_calls(provider, memory_writer):
    """The same EventContext instance is reused for multiple invocations."""
    contexts = []

    async def on_event(_event: dict, ctx: EventContext) -> None:
        contexts.append(id(ctx))

    handler = make_event_handler(provider, memory_writer, {"ping": on_event})
    handler({"event_type": "ping", "data": {}})
    handler({"event_type": "ping", "data": {}})
    assert contexts[0] == contexts[1]


# ── Audit log emission (consumed by admin console Events page) ───────────────


def test_handler_emits_audit_marker_on_success(provider, memory_writer, caplog):
    """Successful invocations must emit a single ``SOCIALAGENT_EVENT`` structured log line."""
    import json as _json
    import logging

    async def on_event(_event: dict, _ctx: EventContext) -> None:
        return None

    handler = make_event_handler(provider, memory_writer, {"order_created": on_event})
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.event"):
        handler(
            {
                "event_type": "order_created",
                "phone": "+521234",
                "data": {"order_id": "O-1", "amount": 99},
            }
        )

    markers = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert len(markers) == 1, f"expected one SOCIALAGENT_EVENT marker, got {markers}"
    payload = _json.loads(markers[0].split("SOCIALAGENT_EVENT ", 1)[1])
    assert payload["event_type"] == "order_created"
    assert payload["status"] == "success"
    assert payload["phone"] == "+...1234"  # by its last four digits
    assert "data.keys=" in payload["summary"]


def test_handler_emits_audit_marker_on_unknown_event_type(
    provider, memory_writer, caplog
):
    """Unknown event types are audited so they surface on the admin UI."""
    import json as _json
    import logging

    handler = make_event_handler(provider, memory_writer, {})
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.event"):
        handler({"event_type": "mystery", "data": {}})

    markers = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert len(markers) == 1
    payload = _json.loads(markers[0].split("SOCIALAGENT_EVENT ", 1)[1])
    assert payload["event_type"] == "mystery"
    assert payload["status"] == "unknown_event_type"


def test_handler_emits_audit_marker_on_handler_failure(provider, memory_writer, caplog):
    """Exceptions from user handlers are audited as ``failed`` before re-raising."""
    import json as _json
    import logging

    async def on_event(_event: dict, _ctx: EventContext) -> None:
        raise RuntimeError("boom")

    handler = make_event_handler(provider, memory_writer, {"bad_event": on_event})
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.event"):
        with pytest.raises(RuntimeError, match="boom"):
            handler({"event_type": "bad_event", "data": {}})

    markers = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert len(markers) == 1
    payload = _json.loads(markers[0].split("SOCIALAGENT_EVENT ", 1)[1])
    assert payload["event_type"] == "bad_event"
    assert payload["status"] == "failed"


def test_handler_audit_marker_detects_sns_source(provider, memory_writer, caplog):
    """Requests wrapped in an SNS envelope should be tagged ``API Gateway``."""
    import json as _json
    import logging

    async def on_event(_event: dict, _ctx: EventContext) -> None:
        return None

    handler = make_event_handler(provider, memory_writer, {"ping": on_event})
    sns_event = {
        "Records": [
            {"Sns": {"Message": _json.dumps({"event_type": "ping", "data": {}})}}
        ]
    }
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.event"):
        handler(sns_event)

    markers = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert len(markers) == 1
    payload = _json.loads(markers[0].split("SOCIALAGENT_EVENT ", 1)[1])
    assert payload["source"] == "API Gateway"


def test_handler_audit_marker_direct_source(provider, memory_writer, caplog):
    """Plain Lambda invocations (no SNS envelope) should be tagged ``Direct``."""
    import json as _json
    import logging

    async def on_event(_event: dict, _ctx: EventContext) -> None:
        return None

    handler = make_event_handler(provider, memory_writer, {"ping": on_event})
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.event"):
        handler({"event_type": "ping", "data": {}})

    markers = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert len(markers) == 1
    payload = _json.loads(markers[0].split("SOCIALAGENT_EVENT ", 1)[1])
    assert payload["source"] == "Direct"


# ── config-sourced scheduler wiring (no os.environ) ────────────────────────────


@pytest.mark.asyncio
async def test_schedule_uses_config_not_env(
    provider, memory_writer, monkeypatch
) -> None:
    """EventContext.schedule sources target/role/prefix/region from SocialAgentConfig.

    Env vars (which the old code read) are set to bogus values to prove they
    are ignored now that config is threaded through.
    """
    monkeypatch.setenv("NOTIFICATION_SERVICE_ARN", "ENV_IGNORED")
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "ENV_IGNORED")
    monkeypatch.setenv("SCHEDULER_PREFIX", "ENV_IGNORED")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    cfg = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="m",
        provider="eum_social",
        phone_number_id="p",
        region="us-west-2",
        _notification_lambda_arn="notif-arn",
        _scheduler_role_arn="role-arn",
        _scheduler_prefix="dev-socialagent",
    )
    ctx = EventContext(provider, memory_writer, None, config=cfg)

    # send_text, as the notification Lambda runs it: with no scheduler Lambda
    # in this config, anything else is refused before schedule_operation.
    with patch(
        "socialagent.handlers.event.schedule_operation",
        new=AsyncMock(return_value="sched-1"),
    ) as sched:
        sid = await ctx.schedule(
            "send_text", "+521", delay_minutes=5, data={"text": "hi"}
        )

    assert sid == "sched-1"
    kwargs = sched.call_args.kwargs
    assert kwargs["target_arn"] == "notif-arn"
    assert kwargs["role_arn"] == "role-arn"
    assert kwargs["prefix"] == "dev-socialagent"
    assert kwargs["region"] == "us-west-2"  # config wins over AWS_REGION env


@pytest.mark.asyncio
async def test_cancel_schedule_uses_config(provider, memory_writer) -> None:
    cfg = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="m",
        provider="eum_social",
        phone_number_id="p",
        region="us-west-2",
        _scheduler_prefix="dev-socialagent",
    )
    ctx = EventContext(provider, memory_writer, None, config=cfg)
    with patch("socialagent.handlers.event.cancel_schedule", new=AsyncMock()) as cancel:
        await ctx.cancel_schedule("sched-1")
    kwargs = cancel.call_args.kwargs
    assert kwargs["prefix"] == "dev-socialagent"
    assert kwargs["region"] == "us-west-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reminder", "send_text"])
async def test_schedule_without_config_is_refused(
    provider, memory_writer, monkeypatch, operation
) -> None:
    """With no config (the unit-test default) there is no Lambda to target, and
    os.environ is not read for one. schedule() refuses with an error that names
    the cause, where botocore failed on the empty Target.Arn with an opaque
    ParamValidationError before any request went out."""
    monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", "ENV_IGNORED")
    ctx = EventContext(provider, memory_writer, None)  # no config
    with patch(
        "socialagent.handlers.event.schedule_operation", new=AsyncMock(return_value="s")
    ) as sched:
        with pytest.raises(
            SchedulerUnavailableError, match="without a SocialAgentConfig"
        ):
            await ctx.schedule(operation, "+521", delay_minutes=5, data={"text": "x"})
    sched.assert_not_called()


# ── schedule target: the scheduler Lambda when the stack has one ───────────────

_NOTIF_ARN = "arn:aws:lambda:us-east-1:123456789012:function:dev-pa-notification"
_SCHED_ARN = "arn:aws:lambda:us-east-1:123456789012:function:dev-pa-scheduler"


def _ctx_from_env(provider, memory_writer, monkeypatch, scheduler_env):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:x")
    monkeypatch.setenv("MEMORY_ID", "m")
    monkeypatch.setenv("NOTIFICATION_SERVICE_ARN", _NOTIF_ARN)
    if scheduler_env is None:
        monkeypatch.delenv("SCHEDULER_LAMBDA_ARN", raising=False)
    else:
        monkeypatch.setenv("SCHEDULER_LAMBDA_ARN", scheduler_env)
    return EventContext(
        provider, memory_writer, None, config=SocialAgentConfig.from_env()
    )


@pytest.mark.asyncio
async def test_schedule_targets_the_scheduler_lambda_when_there_is_one(
    provider, memory_writer, monkeypatch
) -> None:
    # Only the scheduler Lambda runs @app.on_schedule handlers; the
    # notification Lambda answers 400 to any operation it does not know.
    ctx = _ctx_from_env(provider, memory_writer, monkeypatch, _SCHED_ARN)
    with patch(
        "socialagent.handlers.event.schedule_operation", new=AsyncMock(return_value="s")
    ) as sched:
        await ctx.schedule("order_followup", "+521", delay_minutes=5, data={})
    assert sched.call_args.kwargs["target_arn"] == _SCHED_ARN


# Terraform sets the variable to "" when enable_scheduler_lambda is false: an
# empty value is absent, not an empty Target.Arn.
@pytest.mark.asyncio
@pytest.mark.parametrize("scheduler_env", ["", None])
async def test_without_the_scheduler_lambda_only_the_sends_are_scheduled(
    provider, memory_writer, monkeypatch, scheduler_env
) -> None:
    ctx = _ctx_from_env(provider, memory_writer, monkeypatch, scheduler_env)
    with patch(
        "socialagent.handlers.event.schedule_operation", new=AsyncMock(return_value="s")
    ) as sched:
        with pytest.raises(SchedulerUnavailableError, match="scheduler_lambda: true"):
            await ctx.schedule("order_followup", "+521", delay_minutes=5, data={})
        sched.assert_not_called()
        await ctx.schedule("send_text", "+521", delay_minutes=5, data={"text": "x"})
    assert sched.call_args.kwargs["target_arn"] == _NOTIF_ARN


@pytest.mark.asyncio
async def test_schedule_prefers_an_explicit_scheduler_lambda(
    provider, memory_writer
) -> None:
    cfg = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="m",
        provider="eum_social",
        phone_number_id="p",
        _notification_lambda_arn=_NOTIF_ARN,
        _scheduler_lambda_arn=_SCHED_ARN,
    )
    ctx = EventContext(provider, memory_writer, None, config=cfg)
    with patch(
        "socialagent.handlers.event.schedule_operation", new=AsyncMock(return_value="s")
    ) as sched:
        await ctx.schedule("order_followup", "+521", delay_minutes=5, data={})
    assert sched.call_args.kwargs["target_arn"] == _SCHED_ARN
