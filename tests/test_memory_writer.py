# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for SocialAgentMemoryWriter, focused on timestamp monotonicity.

botocore serializes eventTimestamp to a whole-second unix timestamp, so two
out-of-band writes in the same wall-clock second collided on one value and their
restore order became service-determined. The writer must space its own events by
at least a second, like the upstream session manager does.
"""

from __future__ import annotations

import calendar
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from socialagent.memory.writer import SocialAgentMemoryWriter

PHONE = "+5215555550100"
ROOT = Path(__file__).resolve().parents[1]


def _writer() -> SocialAgentMemoryWriter:
    w = SocialAgentMemoryWriter(memory_id="mem-real")
    w._client = MagicMock()
    return w


def _wire_seconds(client: MagicMock) -> list[int]:
    """Whole-second unix timestamps as botocore would serialize each write."""
    return [
        calendar.timegm(c.kwargs["eventTimestamp"].utctimetuple())
        for c in client.create_event.call_args_list
    ]


@pytest.mark.asyncio
async def test_consecutive_writes_get_distinct_wire_timestamps():
    """Two writes in the same wall-clock second must not serialize equal.

    Pin datetime.now to a fixed instant so both writes would otherwise land on
    the same second, the collision the writer must avoid.
    """
    from datetime import UTC, datetime

    fixed = datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=UTC)
    w = _writer()
    # timedelta is imported by name in the module, so patching `datetime` here
    # (only its .now) leaves the +1s spacing arithmetic intact.
    with patch("socialagent.memory.writer.datetime") as dt:
        dt.now.return_value = fixed
        await w.log_assistant_message(PHONE, "first")
        await w.log_assistant_message(PHONE, "second")

    seconds = _wire_seconds(w._client)
    assert seconds[1] > seconds[0], (
        f"both writes serialized to {seconds}; restore order is now service-determined"
    )


@pytest.mark.asyncio
async def test_distinct_sessions_do_not_drag_each_other_forward():
    """The spacing is per session, so one busy session doesn't future-date another."""
    from datetime import UTC, datetime

    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    w = _writer()
    with patch("socialagent.memory.writer.datetime") as dt:
        dt.now.return_value = fixed
        await w.log_assistant_message("+5215555550100", "a")
        await w.log_assistant_message("+5215555550100", "b")
        await w.log_assistant_message("+5215555550199", "c")

    third = w._client.create_event.call_args_list[2].kwargs["eventTimestamp"]
    assert calendar.timegm(third.utctimetuple()) == calendar.timegm(
        fixed.utctimetuple()
    )


@pytest.mark.asyncio
async def test_dev_mode_never_calls_create_event():
    from socialagent.constants import DEV_MODE_VALUE

    w = SocialAgentMemoryWriter(memory_id=DEV_MODE_VALUE)
    await w.log_assistant_message(PHONE, "hi")  # must not raise or call AWS


class _Raw:
    """Minimal urllib3-style body, the shape botocore's AWSResponse reads."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_kwargs):
        yield self._body


def _throttle_then_succeed(failures: int, seen: list[dict], headers=None):
    """A before-send hook: answer ``failures`` throttles, then a success."""
    from botocore.awsrequest import AWSResponse

    def hook(request, **_kwargs):
        seen.append(json.loads(request.body))
        if len(seen) <= failures:
            return AWSResponse(
                request.url,
                429,
                {"x-amzn-ErrorType": "ThrottledException", **(headers or {})},
                _Raw(b'{"message": "Rate exceeded"}'),
            )
        return AWSResponse(request.url, 200, {}, _Raw(b'{"event": {}}'))

    return hook


@pytest.fixture
def real_client_writer(monkeypatch, tmp_path):
    """A writer with its real boto3 client, offline and without backoff sleeps.

    No config file either: one that sets retry_mode and max_attempts would let
    the bounded-retry test pass without the writer's own Config.
    """
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr("boto3.DEFAULT_SESSION", None)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
    monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return SocialAgentMemoryWriter(
        memory_id="test_memory-a1b2c3d4e5", region="us-east-1"
    )


@pytest.mark.asyncio
async def test_throttled_write_is_retried_with_one_idempotency_token(
    real_client_writer,
):
    """A throttled write is retried by the client, as the same event.

    The handlers log a failed write and never re-send the message, so the
    client's retry is the only one the write gets; it must reuse the
    clientToken so a retry of a request that did land cannot add a second event.
    """
    seen: list[dict] = []
    real_client_writer._client.meta.events.register(
        "before-send.bedrock-agentcore.CreateEvent", _throttle_then_succeed(2, seen)
    )

    await real_client_writer.log_assistant_message(PHONE, "Reminder")

    assert len(seen) == 3
    assert len({body["clientToken"] for body in seen}) == 1


@pytest.mark.asyncio
async def test_write_retries_stay_bounded(real_client_writer):
    """Retries stop after three attempts, so the backoff between them stays bounded.

    A Lambda timeout re-runs the invocation and sends the message again, which is
    worse than the missing memory event that a raised error leaves.
    """
    from botocore.exceptions import ClientError

    seen: list[dict] = []
    real_client_writer._client.meta.events.register(
        "before-send.bedrock-agentcore.CreateEvent", _throttle_then_succeed(99, seen)
    )

    with pytest.raises(ClientError):
        await real_client_writer.log_assistant_message(PHONE, "Reminder")
    assert len(seen) == 3


def _lambda_timeouts() -> dict[str, int]:
    """The timeout of each stack Lambda, from infra-terraform/modules/lambdas."""
    text = (ROOT / "infra-terraform" / "modules" / "lambdas" / "main.tf").read_text(
        encoding="utf-8"
    )
    blocks = re.findall(
        r'^resource "aws_lambda_function" "(\w+)" \{\n(.*?)^\}', text, re.M | re.S
    )
    return {
        name: int(m.group(1))
        for name, body in blocks
        if (m := re.search(r"^\s*timeout\s*=\s*(\d+)", body, re.M))
    }


def _worst_backoff_s(attempts: int) -> float:
    """The most the retries can sleep, x-amz-retry-after included.

    Standard mode sleeps rand(0, 1) * 2**(n - 1) before retry n. botocore's
    opt-in new retries (AWS_NEW_RETRIES_2026) also honour x-amz-retry-after, up
    to a fixed number of seconds more per retry; the layer installs whichever
    botocore is newest, so the budget assumes that mode.
    """
    from botocore.retries.standard import ExponentialBackoff

    extra = getattr(ExponentialBackoff, "_RETRY_AFTER_MAX_ADDITIONAL", 5)
    return sum(2 ** (n - 1) + extra for n in range(1, attempts))


@pytest.mark.asyncio
async def test_retry_after_stays_inside_the_budgeted_backoff(
    real_client_writer, monkeypatch
):
    """Throttles that send x-amz-retry-after sleep no longer than the budget allows."""
    import botocore.retries.standard as standard

    monkeypatch.setattr(standard, "NEW_RETRIES_ENABLED", True, raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    seen: list[dict] = []
    real_client_writer._client.meta.events.register(
        "before-send.bedrock-agentcore.CreateEvent",
        _throttle_then_succeed(2, seen, {"x-amz-retry-after": "60000"}),
    )

    await real_client_writer.log_assistant_message(PHONE, "Reminder")

    assert len(seen) == 3
    assert sum(sleeps) > 5, "x-amz-retry-after was not honoured; the test is moot"
    attempts = real_client_writer._client.meta.config.retries["total_max_attempts"]
    assert sum(sleeps) <= _worst_backoff_s(attempts)


def test_a_hung_write_gives_up_inside_the_lambda_timeout(real_client_writer):
    """Every attempt at its connect and read timeouts, plus the backoff, ends in time.

    A Lambda timeout re-runs the invocation and sends the message again. Five
    seconds are left for the Lambda's start-up and the send before the write.
    """
    cfg = real_client_writer._client.meta.config
    attempts = cfg.retries["total_max_attempts"]
    worst = attempts * (cfg.connect_timeout + cfg.read_timeout)
    worst += _worst_backoff_s(attempts)
    timeouts = _lambda_timeouts()
    for name in ("event", "scheduler", "notification"):
        assert name in timeouts, (
            f"no timeout for aws_lambda_function.{name} in modules/lambdas/main.tf; "
            f"found {sorted(timeouts)}"
        )
        assert worst + 5 <= timeouts[name], (
            f"a hung write takes up to {worst}s; the {name} Lambda times out at "
            f"{timeouts[name]}s"
        )
