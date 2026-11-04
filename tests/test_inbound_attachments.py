# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Inbound attachments: the webhook stages an image or document in S3 and lists it
in the runtime payload, so the agent can receive the original instead of a text
description. Opt-in; with the flag off the payload is byte-for-byte what it was.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialagent.config import SocialAgentConfig
from socialagent.handlers.webhook import _build_socialagent_payload
from socialagent.media.attachments import (
    ATTACHMENTS_PREFIX,
    attachment_key,
    stage_attachment,
)
from socialagent.types import InboundMessage, MediaContent, MessageType

_TS = datetime.now(tz=UTC)


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        _transcription_bucket="media-bucket",
        _media_prefix="stacks/demo",
        **kwargs,
    )


def _image_msg(caption: str | None = "a receipt") -> InboundMessage:
    return InboundMessage(
        id="wamid.ABC==",
        sender="+525555550100",
        timestamp=_TS,
        type=MessageType.IMAGE,
        text="",
        media=MediaContent(id="media-1", mime_type="image/jpeg", caption=caption),
    )


def _doc_msg() -> InboundMessage:
    return InboundMessage(
        id="wamid.DOC",
        sender="+525555550100",
        timestamp=_TS,
        type=MessageType.DOCUMENT,
        text="",
        media=MediaContent(
            id="media-2", mime_type="application/pdf", filename="invoice.pdf"
        ),
    )


# --- key derivation -----------------------------------------------------------


def test_key_is_derived_from_message_id_and_mime_never_from_user_input():
    key = attachment_key("stacks/demo", "wamid.ABC==/../x", "image/jpeg")
    assert key.startswith(f"stacks/demo/{ATTACHMENTS_PREFIX}")
    assert key.endswith(".jpeg")
    assert "/.." not in key and "==" not in key.rsplit("/", 1)[1].split(".")[0]


def test_key_without_prefix_has_no_leading_slash():
    key = attachment_key("", "wamid.1", "application/pdf")
    assert key == f"{ATTACHMENTS_PREFIX}wamid.1.pdf"


def test_unknown_mime_is_not_staged():
    assert attachment_key("p", "wamid.1", "application/x-msdownload") is None


# --- staging ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_is_staged_and_described():
    provider = MagicMock(download_media=AsyncMock(return_value=b"\xff\xd8jpeg"))
    s3 = MagicMock()
    att = await stage_attachment(_image_msg(), _config(), provider, s3=s3)
    assert att == {
        "kind": "image",
        "format": "jpeg",
        "s3": {
            "bucket": "media-bucket",
            "key": f"stacks/demo/{ATTACHMENTS_PREFIX}wamid.ABC.jpeg",
        },
        "filename": None,
        "caption": "a receipt",
        "size": 6,
    }
    s3.put_object.assert_called_once()
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "media-bucket"
    assert kwargs["Body"] == b"\xff\xd8jpeg"
    assert kwargs["ContentType"] == "image/jpeg"


@pytest.mark.asyncio
async def test_document_keeps_filename_and_format():
    provider = MagicMock(download_media=AsyncMock(return_value=b"%PDF-1.4"))
    att = await stage_attachment(_doc_msg(), _config(), provider, s3=MagicMock())
    assert att["kind"] == "document"
    assert att["format"] == "pdf"
    assert att["filename"] == "invoice.pdf"


@pytest.mark.asyncio
async def test_over_cap_is_not_staged_and_not_uploaded():
    provider = MagicMock(download_media=AsyncMock(return_value=b"x" * 11))
    s3 = MagicMock()
    att = await stage_attachment(
        _image_msg(), _config(attachments_max_bytes=10), provider, s3=s3
    )
    assert att is None
    s3.put_object.assert_not_called()


@pytest.mark.asyncio
async def test_download_failure_yields_no_attachment():
    provider = MagicMock(download_media=AsyncMock(side_effect=RuntimeError("boom")))
    att = await stage_attachment(_image_msg(), _config(), provider, s3=MagicMock())
    assert att is None


@pytest.mark.asyncio
async def test_no_bucket_configured_yields_no_attachment():
    provider = MagicMock(download_media=AsyncMock(return_value=b"x"))
    cfg = _config()
    object.__setattr__(cfg, "_transcription_bucket", "")
    att = await stage_attachment(_image_msg(), cfg, provider, s3=MagicMock())
    assert att is None
    provider.download_media.assert_not_called()


@pytest.mark.asyncio
async def test_text_message_is_not_an_attachment():
    msg = InboundMessage(
        id="w", sender="+1", timestamp=_TS, type=MessageType.TEXT, text="hola"
    )
    att = await stage_attachment(msg, _config(), MagicMock(), s3=MagicMock())
    assert att is None


# --- payload shape ------------------------------------------------------------


def test_payload_without_attachments_is_unchanged():
    payload = _build_socialagent_payload(
        _image_msg(), _config(), "[User sent an image]"
    )
    assert "attachments" not in payload
    assert payload["prompt"] == "[User sent an image]"


def test_payload_lists_attachments_and_keeps_prompt():
    att = {"kind": "image", "format": "jpeg", "s3": {"bucket": "b", "key": "k"}}
    payload = _build_socialagent_payload(
        _image_msg(), _config(), "[User sent an image]", attachments=[att]
    )
    assert payload["attachments"] == [att]
    assert payload["prompt"] == "[User sent an image]"
    assert payload["socialagent"]["version"] == 1


@pytest.mark.asyncio
async def test_staging_passes_the_cap_to_the_provider_and_handles_too_large():
    """The provider enforces the cap before reading; staging hands it the cap
    and treats MediaTooLarge as not-staged, with a log line."""
    from socialagent.providers.eum_social import MediaTooLarge

    provider = MagicMock(
        download_media=AsyncMock(side_effect=MediaTooLarge(size=99, max_bytes=10))
    )
    cfg = _config(attachments_max_bytes=10)
    out = await stage_attachment(_image_msg(), cfg, provider, s3=MagicMock())
    assert out is None
    provider.download_media.assert_awaited_once()
    assert provider.download_media.await_args.kwargs.get("max_bytes") == 10
