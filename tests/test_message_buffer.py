# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the deterministic (timer-only) message-buffer in the control layer.

The buffer lives in src/socialagent/buffer/ and is configured through
SocialAgentConfig. Buffering is purely timer-based. These tests drive a
MessageBuffer built from a SocialAgentConfig,
covering the dev-mode (file-backed) store, ingest/should_flush decision logic,
combine_prompt, the is_enabled gate, and SocialAgentConfig.from_env tunables.

All run fully offline — the store falls back to a temp JSON file whenever the
state table name is empty (dev mode).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.buffer import BufferStore, MessageBuffer, combine_prompt, store
from socialagent.config import SocialAgentConfig

PHONE = "+5215555550100"


def _config(
    *,
    state_table: str = "",
    enabled: bool = False,
    window: int = 5,
    max_wait: int = 45,
    ttl: int = 900,
) -> SocialAgentConfig:
    """Build a minimal SocialAgentConfig for buffer tests.

    Default ``state_table=""`` puts the BufferStore in dev-mode (temp-file) so
    tests run offline without touching DynamoDB.
    """
    return SocialAgentConfig(
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:0:runtime/test",
        memory_id="mem-test",
        provider="eum_social",
        phone_number_id="pn-test",
        message_buffering_enabled=enabled,
        buffer_window_seconds=window,
        buffer_max_wait_seconds=max_wait,
        buffer_ttl_seconds=ttl,
        _state_table_name=state_table,
    )


@pytest.fixture(autouse=True)
def _clean_dev_store():
    """Ensure a clean dev-store file before and after every test."""
    if store._DEV_PATH.exists():
        store._DEV_PATH.unlink()
    yield
    if store._DEV_PATH.exists():
        store._DEV_PATH.unlink()


# ── combine_prompt (free function, no config) ──────────────────────────────────


def test_combine_prompt_joins_text_with_newlines() -> None:
    msgs = [{"type": "text", "text": "hola"}, {"type": "text", "text": "quiero info"}]
    assert combine_prompt(msgs) == "hola\nquiero info"


def test_combine_prompt_dedups_consecutive_identical_text() -> None:
    msgs = [
        {"type": "text", "text": "hola"},
        {"type": "text", "text": "hola"},
        {"type": "text", "text": "info"},
    ]
    assert combine_prompt(msgs) == "hola\ninfo"


def test_combine_prompt_placeholders_for_non_text() -> None:
    msgs = [
        {"type": "text", "text": "look"},
        {"type": "image"},
        {"type": "audio"},
        {"type": "location"},
        {"type": "document"},
    ]
    out = combine_prompt(msgs, image_prompt="[IMG]")
    assert (
        out
        == "look\n[IMG]\n[User sent a voice note]\n[User shared location]\n[User sent document]"
    )


def test_combine_prompt_button_text_included() -> None:
    assert combine_prompt([{"type": "button", "text": "Confirmar"}]) == "Confirmar"


def test_combine_prompt_button_empty_is_skipped() -> None:
    assert (
        combine_prompt([{"type": "text", "text": "hi"}, {"type": "button", "text": ""}])
        == "hi"
    )


# ── BufferStore: DynamoDB (production) path ────────────────────────────────────
# Mirrors tests/test_state.py — patch boto3.resource and assert on the Table mock.
# The Table is cached in BufferStore.__init__, so construct inside the patch.


@pytest.mark.asyncio
async def test_store_next_seq_uses_atomic_add() -> None:
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"latest_seq": 3}}
    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore(
            table_name="dev-socialagent-state", region="us-east-1", ttl_seconds=900
        )
        seq = await s.next_seq(PHONE)
    assert seq == 3
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"pk": f"buf#{PHONE}", "sk": "meta"}
    assert "ADD latest_seq :one" in kwargs["UpdateExpression"]


@pytest.mark.asyncio
async def test_store_append_and_collect_dynamodb() -> None:
    table = MagicMock()
    item = {"seq": 1, "message_id": "m1", "text": "hi", "ts": 1.0, "type": "text"}
    table.query.return_value = {"Items": [{"data": json.dumps(item)}]}
    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore("dev-socialagent-state", "us-east-1", 900)
        await s.append_message(PHONE, 1, "m1", "hi", 1.0)
        msgs = await s.collect_messages(PHONE)
    put = table.put_item.call_args.kwargs["Item"]
    assert put["pk"] == f"buf#{PHONE}"
    assert put["sk"].startswith("msg#")
    assert msgs == [item]


@pytest.mark.asyncio
async def test_store_clear_batch_deletes_dynamodb() -> None:
    table = MagicMock()
    table.query.return_value = {"Items": [{"sk": "meta"}, {"sk": "msg#0001#m1"}]}
    batch = MagicMock()
    table.batch_writer.return_value.__enter__.return_value = batch
    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore("dev-socialagent-state", "us-east-1", 900)
        await s.clear(PHONE)
    assert batch.delete_item.call_count == 2


@pytest.mark.asyncio
async def test_clear_flushed_deletes_only_what_was_flushed() -> None:
    """A flush must retire its own snapshot, not the whole partition.

    A flush snapshots the buffer, then invokes the runtime for 5-30s before
    clearing. Purging every sk at clear time deletes messages appended during that
    window — a follow-up the user sent while the bot was "typing" — so they are
    never answered and never retried. Delete exactly the consumed keys instead.
    """
    table = MagicMock()
    batch = MagicMock()
    table.batch_writer.return_value.__enter__.return_value = batch
    flushed = [
        {"seq": 1, "message_id": "m1", "text": "a", "ts": 1.0, "type": "text"},
        {"seq": 2, "message_id": "m2", "text": "b", "ts": 2.0, "type": "text"},
    ]

    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore("dev-socialagent-state", "us-east-1", 900)
        await s.clear_messages(PHONE, flushed)

    deleted = [c.kwargs["Key"]["sk"] for c in batch.delete_item.call_args_list]
    assert deleted == [f"msg#{1:020d}#m1", f"msg#{2:020d}#m2"]
    # No query: the keys are derived from the snapshot, so nothing else is even
    # looked at — a message appended meanwhile cannot be caught in the blast.
    table.query.assert_not_called()


@pytest.mark.asyncio
async def test_clear_flushed_leaves_the_generation_stamp() -> None:
    """`meta` must survive, or latest_seq resets to 0 and flushes go stale.

    clear() deleted the meta item, so the next burst restarted at seq 1 while
    flushes queued under the old numbering were still in flight. Such a stale
    flush then saw `current(1) > scheduled(2)` as false and drained the new burst
    before its debounce window elapsed — splitting one thought into two replies,
    the exact failure the buffer exists to prevent. The item carries a TTL, so
    leaving it costs nothing.
    """
    table = MagicMock()
    batch = MagicMock()
    table.batch_writer.return_value.__enter__.return_value = batch

    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore("dev-socialagent-state", "us-east-1", 900)
        await s.clear_messages(
            PHONE, [{"seq": 1, "message_id": "m1", "text": "a", "ts": 1.0}]
        )

    deleted = [c.kwargs["Key"]["sk"] for c in batch.delete_item.call_args_list]
    assert "meta" not in deleted


@pytest.mark.asyncio
async def test_clear_flushed_with_no_messages_is_a_noop() -> None:
    table = MagicMock()
    with patch("socialagent.buffer.store.boto3.resource") as res:
        res.return_value.Table.return_value = table
        s = BufferStore("dev-socialagent-state", "us-east-1", 900)
        await s.clear_messages(PHONE, [])
    table.batch_writer.assert_not_called()


@pytest.mark.asyncio
async def test_buffer_clear_flushed_delegates_to_store() -> None:
    buf = MessageBuffer(_config())
    buf._store = MagicMock()
    buf._store.clear_messages = AsyncMock()
    msgs = [{"seq": 1, "message_id": "m1", "text": "a", "ts": 1.0}]

    await buf.clear_flushed(PHONE, msgs)

    buf._store.clear_messages.assert_awaited_once_with(PHONE, msgs)


# ── ingest: always flush_now=False, seq increments, appends ────────────────────


@pytest.mark.asyncio
async def test_ingest_text_never_flushes_now_and_increments_seq() -> None:
    buf = MessageBuffer(_config())
    r1 = await buf.ingest_text(PHONE, "wamid.1", "hola", now=100.0)
    r2 = await buf.ingest_text(PHONE, "wamid.2", "info", now=101.0)

    assert (r1.seq, r1.flush_now) == (1, False)
    assert (r2.seq, r2.flush_now) == (2, False)

    msgs = await buf.collect_messages(PHONE)
    assert [m["text"] for m in msgs] == ["hola", "info"]
    assert [m["seq"] for m in msgs] == [1, 2]


# ── should_flush decision logic ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_should_flush_empty_buffer() -> None:
    d = await MessageBuffer(_config()).should_flush(PHONE, scheduled_seq=1, now=200.0)
    assert d.should_flush is False
    assert d.reason == "buffer_empty"


@pytest.mark.asyncio
async def test_should_flush_window_elapsed_when_seq_current() -> None:
    buf = MessageBuffer(_config())
    await buf.ingest_text(PHONE, "wamid.1", "hola", now=100.0)
    d = await buf.should_flush(PHONE, scheduled_seq=1, now=105.0)
    assert d.should_flush is True
    assert d.reason == "window_elapsed"
    assert [m["text"] for m in d.messages] == ["hola"]


@pytest.mark.asyncio
async def test_should_flush_superseded_when_newer_message_arrived() -> None:
    buf = MessageBuffer(_config())
    await buf.ingest_text(PHONE, "wamid.1", "hola", now=100.0)
    await buf.ingest_text(PHONE, "wamid.2", "mas", now=101.0)  # latest_seq = 2
    d = await buf.should_flush(PHONE, scheduled_seq=1, now=103.0)
    assert d.should_flush is False
    assert d.reason == "superseded"


@pytest.mark.asyncio
async def test_should_flush_max_wait_overrides_superseded() -> None:
    buf = MessageBuffer(_config(max_wait=45))
    await buf.ingest_text(PHONE, "wamid.1", "hola", now=100.0)
    await buf.ingest_text(PHONE, "wamid.2", "mas", now=101.0)  # latest_seq = 2
    # Newer message exists, but the oldest buffered message is older than the
    # 45s max-wait cap → flush anyway so a chatty user still gets a response.
    d = await buf.should_flush(PHONE, scheduled_seq=1, now=100.0 + 46)
    assert d.should_flush is True
    assert d.reason == "max_wait_exceeded"
    assert len(d.messages) == 2


# ── clear resets the buffer + generation token ─────────────────────────────────


@pytest.mark.asyncio
async def test_clear_resets_buffer_and_seq() -> None:
    buf = MessageBuffer(_config())
    await buf.ingest_text(PHONE, "wamid.1", "hola", now=100.0)
    await buf.clear(PHONE)
    assert await buf.collect_messages(PHONE) == []
    # A fresh ingest after clear restarts the sequence at 1.
    r = await buf.ingest_text(PHONE, "wamid.2", "again", now=200.0)
    assert r.seq == 1


# ── is_enabled feature gate ────────────────────────────────────────────────────


def test_is_enabled_false_in_dev_mode() -> None:
    # No state table → dev mode → always disabled even if the flag is on.
    assert MessageBuffer(_config(state_table="", enabled=True)).is_enabled() is False


def test_is_enabled_true_when_table_set_and_flag_on() -> None:
    cfg = _config(state_table="dev-socialagent-state", enabled=True)
    # A live deployment always has the flush queue; buffering without it is the
    # broken state covered by test_is_enabled_false_without_a_flush_queue.
    cfg._buffer_queue_url = "https://sqs.us-east-1.amazonaws.com/111122223333/q"
    assert MessageBuffer(cfg).is_enabled() is True


def test_is_enabled_false_when_flag_off() -> None:
    assert (
        MessageBuffer(
            _config(state_table="dev-socialagent-state", enabled=False)
        ).is_enabled()
        is False
    )


def test_window_seconds_property_exposes_config() -> None:
    assert MessageBuffer(_config(window=12)).window_seconds == 12


# ── SocialAgentConfig.from_env reads the buffer tunables ──────────────────────────────


def test_from_env_reads_buffer_tunables(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:test")
    monkeypatch.setenv("MEMORY_ID", "mem-test")
    monkeypatch.setenv("MESSAGE_BUFFERING_ENABLED", "true")
    monkeypatch.setenv("BUFFER_WINDOW_SECONDS", "12")
    monkeypatch.setenv("BUFFER_MAX_WAIT_SECONDS", "90")
    monkeypatch.setenv("BUFFER_TTL_SECONDS", "120")
    monkeypatch.setenv("BUFFER_QUEUE_URL", "https://sqs.example/q")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MEDIA_PREFIX", "stacks/x")
    monkeypatch.setenv("SOCIALAGENT_META_PHONE_NUMBER_ID", "PID123")

    cfg = SocialAgentConfig.from_env()
    assert cfg.message_buffering_enabled is True
    assert cfg.buffer_window_seconds == 12
    assert cfg.buffer_max_wait_seconds == 90
    assert cfg.buffer_ttl_seconds == 120
    assert cfg._buffer_queue_url == "https://sqs.example/q"
    # config-centralization fields folded in from scattered env reads
    assert cfg.log_level == "DEBUG"
    assert cfg._media_prefix == "stacks/x"
    assert cfg.meta_phone_number_id == "PID123"


def test_from_env_defaults_and_window_cap(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:test")
    monkeypatch.setenv("MEMORY_ID", "mem-test")
    for var in (
        "MESSAGE_BUFFERING_ENABLED",
        "BUFFER_WINDOW_SECONDS",
        "BUFFER_MAX_WAIT_SECONDS",
        "BUFFER_TTL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BUFFER_WINDOW_SECONDS", "5000")  # over the SQS DelaySeconds cap

    cfg = SocialAgentConfig.from_env()
    assert cfg.message_buffering_enabled is False  # default off
    assert cfg.buffer_window_seconds == 900  # capped at SQS maximum
    assert cfg.buffer_max_wait_seconds == 45  # default
    assert cfg.buffer_ttl_seconds == 900  # default


# ── is_enabled must require every dependency the flush path needs ──────────────


def test_is_enabled_false_without_a_flush_queue() -> None:
    """Buffering with no queue URL accepts messages and never answers them.

    ingest succeeds, send_flush silently no-ops when the URL is unset, and the
    handler returns "buffered" after logging that a flush was scheduled — so every
    text message is swallowed with no error anywhere. Falling back to unbuffered
    handling is the safe degradation: a slightly chattier bot beats a silent one.
    """
    cfg = _config(state_table="dev-socialagent-state", enabled=True)
    cfg._buffer_queue_url = ""
    assert MessageBuffer(cfg).is_enabled() is False
