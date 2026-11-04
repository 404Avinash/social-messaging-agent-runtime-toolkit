# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Media prompt resolution — empty-prompt backstop (silent voice notes).

A silent / too-short voice note makes Amazon Transcribe COMPLETE the job with
an empty transcript. Forwarding "" to the runtime crashes it with a 500 (Bedrock
rejects an empty ContentBlock). These tests pin the guards that turn an empty
resolved prompt into a neutral placeholder.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.config import SocialAgentConfig
from socialagent.handlers.webhook import _media_prompt, _resolve_prompt
from socialagent.providers.utils import parse_message
from socialagent.types import InboundMessage, MessageType

_TS = datetime.now(tz=UTC)


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        **kwargs,
    )


def _audio_msg(sender: str = "+525555550100") -> InboundMessage:
    return InboundMessage(
        id="wamid.1",
        sender=sender,
        timestamp=_TS,
        type=MessageType.AUDIO,
        text="",
        media=MagicMock(id="media-1", mime_type="audio/ogg; codecs=opus"),
    )


# ── _resolve_prompt backstop ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_prompt_substitutes_placeholder_when_on_media_returns_empty():
    """An on_media hook returning "" must not reach the runtime — the backstop
    substitutes a neutral placeholder (empty ContentBlock would 500 the runtime)."""
    on_media = AsyncMock(return_value="")
    prompt = await _resolve_prompt(_audio_msg(), _config(), MagicMock(), on_media)
    assert prompt == "[User sent audio content with no readable text]"


@pytest.mark.asyncio
async def test_resolve_prompt_substitutes_placeholder_when_on_media_returns_whitespace():
    on_media = AsyncMock(return_value="   \n  ")
    prompt = await _resolve_prompt(_audio_msg(), _config(), MagicMock(), on_media)
    assert prompt == "[User sent audio content with no readable text]"


@pytest.mark.asyncio
async def test_resolve_prompt_passes_through_non_empty_on_media():
    on_media = AsyncMock(return_value="El nombre de mi perrita es Lilis")
    prompt = await _resolve_prompt(_audio_msg(), _config(), MagicMock(), on_media)
    assert prompt == "El nombre de mi perrita es Lilis"


@pytest.mark.asyncio
async def test_resolve_prompt_prefers_text_over_media():
    msg = InboundMessage(
        id="x", sender="+521", timestamp=_TS, type=MessageType.TEXT, text="hola"
    )
    prompt = await _resolve_prompt(msg, _config(), MagicMock(), None)
    assert prompt == "hola"


# ── _media_prompt transcriber guard ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_media_prompt_empty_transcript_falls_back_to_placeholder():
    """Silent note -> Transcribe returns "" -> fall back to the voice-note placeholder."""
    provider = MagicMock()
    provider.download_media = AsyncMock(return_value=b"\x00\x00")
    config = _config(_transcription_bucket="b")
    with patch(
        "socialagent.handlers.webhook.transcribe_audio", AsyncMock(return_value="")
    ):
        prompt = await _media_prompt(_audio_msg(), config, provider)
    assert prompt == "[User sent a voice note]"


@pytest.mark.asyncio
async def test_media_prompt_returns_real_transcript():
    provider = MagicMock()
    provider.download_media = AsyncMock(return_value=b"audio")
    config = _config(_transcription_bucket="b")
    with patch(
        "socialagent.handlers.webhook.transcribe_audio",
        AsyncMock(return_value="hola mundo"),
    ):
        prompt = await _media_prompt(_audio_msg(), config, provider)
    assert prompt == "hola mundo"


@pytest.mark.asyncio
async def test_media_prompt_audio_without_bucket_uses_placeholder():
    prompt = await _media_prompt(_audio_msg(), _config(), MagicMock())
    assert prompt == "[User sent a voice note]"


# ── reactions ─────────────────────────────────────────────────────────────────

_REACTED_ID = "wamid.HBgLNTI1NTU1NTUwMTAw"


def _reaction(emoji: str | None) -> InboundMessage:
    """A reaction as the provider parses it from a real-shaped Meta message.

    ``None`` leaves the ``emoji`` field out.
    """
    reaction = {"message_id": _REACTED_ID}
    if emoji is not None:
        reaction["emoji"] = emoji
    return parse_message(
        {
            "from": "525555550100",
            "id": "wamid.reaction",
            "timestamp": "1760000000",
            "type": "reaction",
            "reaction": reaction,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji", ["👍", "👎"])
async def test_reaction_prompt_carries_the_emoji_and_its_target(emoji: str):
    """The agent must be able to tell a thumbs-up from a thumbs-down."""
    prompt = await _resolve_prompt(_reaction(emoji), _config(), MagicMock(), None)
    assert prompt == f"[User reacted {emoji} to message {_REACTED_ID}]"


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji", ["", None])
async def test_removed_reaction_prompt_says_so(emoji: str | None):
    """A removed reaction carries no emoji: an empty field, or none at all."""
    prompt = await _resolve_prompt(_reaction(emoji), _config(), MagicMock(), None)
    assert prompt == f"[User removed their reaction to message {_REACTED_ID}]"


@pytest.mark.asyncio
async def test_reaction_without_details_keeps_the_generic_prompt():
    msg = InboundMessage(
        id="x", sender="+521", timestamp=_TS, type=MessageType.REACTION
    )
    prompt = await _media_prompt(msg, _config(), MagicMock())
    assert prompt == "[User sent a reaction]"


def _handler_provider(message: InboundMessage) -> MagicMock:
    provider = MagicMock()
    provider.parse_webhook = AsyncMock(return_value=message)
    provider.send_typing = AsyncMock()
    provider.send_text = AsyncMock()
    provider.send_message = AsyncMock()
    return provider


@pytest.mark.parametrize("emoji", ["", None])
def test_removed_reaction_runs_no_agent_turn(emoji: str | None):
    """Taking a reaction back says nothing the agent should answer.

    The parse used to fail on it, so the user got no reply. A turn would now
    reply to it, unasked, through the blueprint hook too.
    """
    from socialagent.handlers.webhook import make_webhook_handler

    provider = _handler_provider(_reaction(emoji))
    on_media = AsyncMock(return_value="[User sent reaction]")
    with patch("socialagent.handlers.webhook._invoke_runtime") as invoke:
        result = make_webhook_handler(_config(), provider, on_media=on_media)({})

    assert result["statusCode"] == 200
    invoke.assert_not_called()
    on_media.assert_not_awaited()
    provider.send_typing.assert_not_awaited()
    provider.send_text.assert_not_awaited()


def test_new_reaction_still_runs_an_agent_turn():
    from socialagent.handlers.webhook import make_webhook_handler

    provider = _handler_provider(_reaction("👍"))
    with patch(
        "socialagent.handlers.webhook._invoke_runtime", return_value=("Gracias", [])
    ) as invoke:
        make_webhook_handler(_config(), provider)({})

    payload = invoke.call_args.args[2]
    assert payload["prompt"] == f"[User reacted 👍 to message {_REACTED_ID}]"
    provider.send_text.assert_awaited_once_with("+525555550100", "Gracias")
