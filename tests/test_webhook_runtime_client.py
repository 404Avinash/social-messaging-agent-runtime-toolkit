# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""How the webhook calls AgentCore Runtime: timeouts, retries, timeout reply.

InvokeAgentRuntime runs a whole agent turn, so it is not idempotent. botocore's
defaults (a 60 s read timeout and legacy retries, which retry a read timeout)
re-sent a slow turn up to five times on the same session, running its tools
again each time, until the 120 s webhook Lambda timed out with nothing sent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

import socialagent.handlers.webhook as webhook
from socialagent.config import SocialAgentConfig
from socialagent.constants import (
    DEFAULT_ERRORS,
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_MIN_WAIT_SECONDS,
    RUNTIME_READ_TIMEOUT_SECONDS,
    RUNTIME_REPLY_RESERVE_SECONDS,
)
from socialagent.types import MessageType

_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc"
_SESSION = "whatsapp-session-conversation-525555550100"


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests are signed before ``before-send`` fires; nothing leaves the host."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


async def _no_sleep(_seconds: float) -> None:
    return None


def _clients_that_time_out(attempts: list[str], created: list) -> object:
    """Wrap boto3.client so every InvokeAgentRuntime send hits a read timeout."""
    real_client = boto3.client

    def client(*args, **kwargs):
        c = real_client(*args, **kwargs)

        def on_send(request, **_):
            attempts.append(request.url)
            raise ReadTimeoutError(endpoint_url=request.url)

        c.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", on_send
        )
        created.append(c)
        return c

    return client


def test_runtime_call_is_sent_once_when_the_reply_is_slow() -> None:
    attempts: list[str] = []
    created: list = []
    with (
        patch.object(boto3, "client", _clients_that_time_out(attempts, created)),
        pytest.raises(ReadTimeoutError),
    ):
        webhook._invoke_runtime(_ARN, _SESSION, {"prompt": "hola"}, "us-east-1")

    assert len(attempts) == 1


def test_runtime_client_waits_long_enough_but_not_forever() -> None:
    attempts: list[str] = []
    created: list = []
    with (
        patch.object(boto3, "client", _clients_that_time_out(attempts, created)),
        pytest.raises(ReadTimeoutError),
    ):
        webhook._invoke_runtime(_ARN, _SESSION, {"prompt": "hola"}, "us-east-1")

    (client,) = created
    assert client.meta.config.connect_timeout == RUNTIME_CONNECT_TIMEOUT_SECONDS
    assert client.meta.config.read_timeout == RUNTIME_READ_TIMEOUT_SECONDS
    assert RUNTIME_READ_TIMEOUT_SECONDS > 60  # longer than the botocore default


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn=_ARN,
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        **kwargs,
    )


def _provider() -> MagicMock:
    p = MagicMock()
    p.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender="+525555550100", type=MessageType.TEXT, text="Hola"
        )
    )
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    return p


@pytest.mark.parametrize(
    "exc",
    [
        ReadTimeoutError(endpoint_url="https://bedrock-agentcore"),
        ConnectTimeoutError(endpoint_url="https://bedrock-agentcore"),
    ],
)
def test_runtime_timeout_sends_the_timeout_message(exc: Exception) -> None:
    provider = _provider()
    with (
        patch.object(webhook, "_invoke_runtime", side_effect=exc),
        patch.object(webhook.asyncio, "sleep", _no_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1})

    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["timeout"]
    )


def test_runtime_timeout_message_is_configurable() -> None:
    provider = _provider()
    config = _config(error_messages={"timeout": "Tardé demasiado, intenta de nuevo."})
    with patch.object(
        webhook,
        "_invoke_runtime",
        side_effect=ReadTimeoutError(endpoint_url="https://bedrock-agentcore"),
    ):
        webhook.make_webhook_handler(config, provider)({"e": 1})

    provider.send_text.assert_called_once_with(
        "+525555550100", "Tardé demasiado, intenta de nuevo."
    )


# ── the wait fits the time the Lambda has left ────────────────────────────────


class _LambdaClock:
    """A Lambda context whose remaining time runs down as the test spends it."""

    def __init__(self, seconds_left: float) -> None:
        self.left = seconds_left

    def get_remaining_time_in_millis(self) -> int:
        return int(self.left * 1000)

    def spend(self, seconds: float) -> None:
        self.left -= seconds


def _voice_note_provider() -> MagicMock:
    p = _provider()
    p.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender="+525555550100", type=MessageType.AUDIO, text=None
        )
    )
    return p


def test_runtime_wait_leaves_room_after_a_long_transcription() -> None:
    """A slow transcription must not push the agent's wait past the Lambda's end.

    With a fixed wait, 25 s of transcription plus 5 + 100 s for the runtime ran
    over the 120 s Lambda timeout, which killed it with no reply at all.
    """
    clock = _LambdaClock(120)

    async def transcribe(_message) -> str:
        clock.spend(25)
        return "¿Qué tiempo hace mañana?"

    attempts: list[str] = []
    created: list = []
    provider = _voice_note_provider()
    with patch.object(boto3, "client", _clients_that_time_out(attempts, created)):
        webhook.make_webhook_handler(_config(), provider, on_media=transcribe)(
            {"e": 1}, clock
        )

    (client,) = created
    assert client.meta.config.read_timeout == pytest.approx(
        120 - 25 - RUNTIME_CONNECT_TIMEOUT_SECONDS - RUNTIME_REPLY_RESERVE_SECONDS
    )
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["timeout"]
    )


def test_runtime_wait_is_capped_when_the_lambda_has_time_to_spare() -> None:
    attempts: list[str] = []
    created: list = []
    with patch.object(boto3, "client", _clients_that_time_out(attempts, created)):
        webhook.make_webhook_handler(_config(), _provider())(
            {"e": 1}, _LambdaClock(900)
        )

    (client,) = created
    assert client.meta.config.read_timeout == RUNTIME_READ_TIMEOUT_SECONDS


def test_no_turn_is_started_without_time_to_answer_it() -> None:
    """A turn nobody can deliver would still run, tools included."""
    provider = _provider()
    left = RUNTIME_CONNECT_TIMEOUT_SECONDS + RUNTIME_REPLY_RESERVE_SECONDS
    with patch.object(boto3, "client") as fake_client:
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, _LambdaClock(left))

    fake_client.assert_not_called()
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["timeout"]
    )


@pytest.mark.parametrize("wait", [1, RUNTIME_MIN_WAIT_SECONDS - 0.5])
def test_no_turn_is_started_with_too_little_time_to_finish_it(wait: float) -> None:
    """A few seconds is too short for a turn, which would still run its tools.

    After a 97 s voice note, a 3 s wait started a turn that called
    schedule_reminder and then timed out, so the user asked again and got a
    second reminder.
    """
    provider = _provider()
    left = RUNTIME_CONNECT_TIMEOUT_SECONDS + RUNTIME_REPLY_RESERVE_SECONDS + wait
    with patch.object(webhook, "_invoke_runtime") as invoke:
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, _LambdaClock(left))

    invoke.assert_not_called()
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["timeout"]
    )


def test_a_turn_starts_with_the_least_time_it_needs() -> None:
    provider = _provider()
    left = (
        RUNTIME_CONNECT_TIMEOUT_SECONDS
        + RUNTIME_REPLY_RESERVE_SECONDS
        + RUNTIME_MIN_WAIT_SECONDS
    )
    with patch.object(webhook, "_invoke_runtime", return_value=("Hola", [])) as invoke:
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, _LambdaClock(left))

    assert invoke.call_args.args[4] == pytest.approx(RUNTIME_MIN_WAIT_SECONDS)
    provider.send_text.assert_called_once_with("+525555550100", "Hola")


def test_buffered_flush_waits_only_as_long_as_the_lambda_has_left() -> None:
    from socialagent.buffer import FlushDecision

    decision = FlushDecision(
        True, "window_elapsed", [{"type": "text", "text": "hola", "message_id": "w1"}]
    )
    buffer = MagicMock()
    buffer.should_flush = AsyncMock(return_value=decision)
    buffer.clear_flushed = AsyncMock()
    event = {
        "Records": [
            {
                "eventSource": "aws:sqs",
                "body": '{"phone": "+525555550100", "seq": 1}',
            }
        ]
    }
    attempts: list[str] = []
    created: list = []
    with (
        patch.object(webhook, "MessageBuffer", return_value=buffer),
        patch.object(boto3, "client", _clients_that_time_out(attempts, created)),
    ):
        webhook.make_webhook_handler(_config(), _provider())(event, _LambdaClock(60))

    (client,) = created
    assert client.meta.config.read_timeout == pytest.approx(
        60 - RUNTIME_CONNECT_TIMEOUT_SECONDS - RUNTIME_REPLY_RESERVE_SECONDS
    )


def test_a_late_reply_with_many_attachments_is_sent_before_the_lambda_ends() -> None:
    """Near the end, the gaps between sends go before the sends themselves do.

    An agent that answered at 98 s with six images to upload needed six 3 s gaps
    and seven sends: past 120 s, so the Lambda was killed mid-drain, and on the
    buffered path SQS then ran the whole turn again.
    """
    clock = _LambdaClock(120)
    image = {
        "operation": "send_message",
        "message": {"type": "image", "payload": {"image": {"id": "m"}}},
    }

    def slow_turn(*_args, **_kwargs):
        clock.spend(98)
        return "Listo", [image] * 6

    sent: list[str] = []

    async def send(*_args, **_kwargs) -> None:
        assert clock.left > 0, "the Lambda had already timed out"
        clock.spend(1)
        sent.append("send")

    async def fake_sleep(seconds: float) -> None:
        clock.spend(seconds)

    provider = _provider()
    provider.send_message = AsyncMock(side_effect=send)
    provider.send_text = AsyncMock(side_effect=send)
    with (
        patch.object(webhook, "_invoke_runtime", side_effect=slow_turn),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, clock)

    assert len(sent) == 7
    assert clock.left > 0


def test_a_long_late_reply_is_sent_in_full_before_the_lambda_ends() -> None:
    """The chunks of a long reply drop their gaps near the end, as attachments do.

    The provider slept the full gap between the 1600-character chunks of a
    reply whatever the Lambda had left, so a 6500-character answer that came
    back with 19 s left, after two images, lost its last chunk to the timeout.
    """
    from socialagent.providers.eum_social import EUMSocialProvider

    clock = _LambdaClock(120)
    reply = "palabra " * 813
    image = {
        "operation": "send_message",
        "message": {"type": "image", "payload": {"image": {"id": "m"}}},
    }

    def slow_turn(*_args, **_kwargs):
        clock.spend(101)
        return reply, [image] * 2

    sent: list[dict] = []

    def send_raw(payload: dict) -> str:
        assert clock.left > 0, "the Lambda had already timed out"
        clock.spend(0.4)
        sent.append(payload)
        return "wamid.out"

    async def fake_sleep(seconds: float) -> None:
        clock.spend(seconds)

    provider = EUMSocialProvider(phone_number_id="ph-x", message_gap_seconds=3)
    provider._send_raw = send_raw
    provider.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender="+525555550100", type=MessageType.TEXT, text="Hola"
        )
    )
    provider.send_typing = AsyncMock()
    with (
        patch.object(webhook, "_invoke_runtime", side_effect=slow_turn),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(outbound_gap_seconds=3), provider)(
            {"e": 1}, clock
        )

    texts = [p["text"]["body"] for p in sent if p["type"] == "text"]
    assert len(texts) == 5
    assert "".join(texts) == reply
    assert clock.left > 0


def test_a_long_reply_keeps_its_gaps_while_there_is_time() -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = _provider()
    with (
        patch.object(webhook, "_invoke_runtime", return_value=("palabra " * 400, [])),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(outbound_gap_seconds=3), provider)(
            {"e": 1}, _LambdaClock(120)
        )

    assert provider.send_text.await_count == 2
    assert slept == [3]


def test_attachments_keep_their_gap_while_there_is_time() -> None:
    clock = _LambdaClock(120)
    button = {
        "operation": "send_message",
        "message": {"type": "interactive", "payload": {}},
    }
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with (
        patch.object(webhook, "_invoke_runtime", return_value=("Ok", [button] * 2)),
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(outbound_gap_seconds=3), _provider())(
            {"e": 1}, clock
        )

    assert slept == [3, 3]


# ── a call that never reached the agent is sent again ─────────────────────────


class _ErrorBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_):
        yield self._body


def _clients_that_are_throttled(attempts: list[str]) -> object:
    """Wrap boto3.client so every InvokeAgentRuntime send gets a 429 back."""
    real_client = boto3.client

    def client(*args, **kwargs):
        c = real_client(*args, **kwargs)

        def on_send(request, **_):
            attempts.append(request.url)
            return AWSResponse(
                request.url,
                429,
                {
                    "x-amzn-ErrorType": "ThrottlingException",
                    "Content-Type": "application/json",
                },
                _ErrorBody(b'{"message": "Rate exceeded"}'),
            )

        c.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", on_send
        )
        return c

    return client


def _throttled() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "InvokeAgentRuntime",
    )


@pytest.mark.parametrize(
    "exc",
    [
        _throttled(),
        EndpointConnectionError(endpoint_url="https://bedrock-agentcore"),
        ConnectTimeoutError(endpoint_url="https://bedrock-agentcore"),
    ],
)
def test_a_call_that_never_reached_the_agent_is_sent_again(exc: Exception) -> None:
    """A throttled call, or one that never connected, ran no turn.

    Turning botocore's retries off, so that a slow reply is not sent again,
    turned them off for these too: a short throttle burst reached the user as
    the busy message instead of passing unnoticed.
    """
    provider = _provider()
    with (
        patch.object(
            webhook, "_invoke_runtime", side_effect=[exc, ("Hola", [])]
        ) as invoke,
        patch.object(webhook.asyncio, "sleep", _no_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1})

    assert invoke.call_count == 2
    provider.send_text.assert_called_once_with("+525555550100", "Hola")


def test_a_throttled_call_is_tried_three_times_then_reported() -> None:
    attempts: list[str] = []
    provider = _provider()
    with (
        patch.object(boto3, "client", _clients_that_are_throttled(attempts)),
        patch.object(webhook.asyncio, "sleep", _no_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1})

    assert len(attempts) == 3
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["throttle"]
    )


def test_a_slow_reply_is_not_sent_again() -> None:
    attempts: list[str] = []
    created: list = []
    provider = _provider()
    with (
        patch.object(boto3, "client", _clients_that_time_out(attempts, created)),
        patch.object(webhook.asyncio, "sleep", _no_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1})

    assert len(attempts) == 1
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["timeout"]
    )


def _clients_that_fail(attempts: list[str], fail) -> object:
    """Wrap boto3.client so every InvokeAgentRuntime send ends in ``fail(url)``."""
    real_client = boto3.client

    def client(*args, **kwargs):
        c = real_client(*args, **kwargs)

        def on_send(request, **_):
            attempts.append(request.url)
            return fail(request.url)

        c.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", on_send
        )
        return c

    return client


def _server_error(url: str) -> AWSResponse:
    return AWSResponse(
        url,
        500,
        {
            "x-amzn-ErrorType": "InternalServerException",
            "Content-Type": "application/json",
        },
        _ErrorBody(b'{"message": "Internal error"}'),
    )


def _dropped_connection(url: str) -> AWSResponse:
    raise ConnectionClosedError(endpoint_url=url)


@pytest.mark.parametrize(
    "fail", [_server_error, _dropped_connection], ids=["5xx", "dropped"]
)
def test_a_call_that_may_have_run_the_turn_is_not_sent_again(fail) -> None:
    """A 5xx, or a connection dropped after the request left, can come after
    AgentCore ran the turn, so a retry could run the turn and its tools again.
    botocore's legacy retries sent such a call five times."""
    attempts: list[str] = []
    provider = _provider()
    with (
        patch.object(boto3, "client", _clients_that_fail(attempts, fail)),
        patch.object(webhook.asyncio, "sleep", _no_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1})

    assert len(attempts) == 1
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["generic"]
    )


def test_a_retry_waits_only_for_the_time_left() -> None:
    clock = _LambdaClock(120)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.spend(seconds)

    provider = _provider()
    with (
        patch.object(
            webhook, "_invoke_runtime", side_effect=[_throttled(), ("Hola", [])]
        ) as invoke,
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, clock)

    first, second = (c.args[4] for c in invoke.call_args_list)
    assert first == RUNTIME_READ_TIMEOUT_SECONDS
    assert 0 < slept[0] <= 1
    assert second == pytest.approx(  # the clock counts whole milliseconds
        120
        - slept[0]
        - RUNTIME_CONNECT_TIMEOUT_SECONDS
        - RUNTIME_REPLY_RESERVE_SECONDS,
        abs=1e-3,
    )


def test_no_retry_once_the_pause_would_leave_no_time_to_wait() -> None:
    # The first pause is at least half a second, so it leaves under the least wait.
    clock = _LambdaClock(
        RUNTIME_CONNECT_TIMEOUT_SECONDS
        + RUNTIME_REPLY_RESERVE_SECONDS
        + RUNTIME_MIN_WAIT_SECONDS
        + 0.4
    )

    async def fake_sleep(seconds: float) -> None:
        clock.spend(seconds)

    provider = _provider()
    with (
        patch.object(webhook, "_invoke_runtime", side_effect=_throttled()) as invoke,
        patch.object(webhook.asyncio, "sleep", fake_sleep),
    ):
        webhook.make_webhook_handler(_config(), provider)({"e": 1}, clock)

    assert invoke.call_count == 1
    provider.send_text.assert_called_once_with(
        "+525555550100", DEFAULT_ERRORS["throttle"]
    )
