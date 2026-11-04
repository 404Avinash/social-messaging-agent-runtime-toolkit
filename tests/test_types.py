# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Core types: InboundMessage, OutboundMessage, MessageType, content types.
"""

from datetime import UTC, datetime


from socialagent.types import (
    ContactContent,
    InboundMessage,
    LocationContent,
    MediaContent,
    MessageType,
    OutboundMessage,
    ReactionContent,
)

_TS = datetime.now(tz=UTC)


# ── MessageType ───────────────────────────────────────────────────────────────


def test_all_message_type_values() -> None:
    expected = {
        "text",
        "image",
        "audio",
        "video",
        "document",
        "location",
        "contact",
        "reaction",
        "button_reply",
        "list_reply",
        "form_reply",
        "sticker",
    }
    assert {m.value for m in MessageType} == expected


def test_message_type_lookup_by_value() -> None:
    assert MessageType("button_reply") == MessageType.BUTTON_REPLY


# ── InboundMessage ────────────────────────────────────────────────────────────


def test_inbound_message_required_fields() -> None:
    msg = InboundMessage(
        id="wamid.1", sender="+521", timestamp=_TS, type=MessageType.TEXT
    )
    assert msg.id == "wamid.1"
    assert msg.sender == "+521"
    assert msg.type == MessageType.TEXT


def test_inbound_message_defaults() -> None:
    msg = InboundMessage(id="x", sender="+1", timestamp=_TS, type=MessageType.TEXT)
    assert msg.text is None
    assert msg.media is None
    assert msg.location is None
    assert msg.contacts is None
    assert msg.reaction is None
    assert msg.reply_to is None
    assert msg.forwarded is False
    assert msg.raw == {}


def test_inbound_message_with_all_fields() -> None:
    media = MediaContent(id="m1", mime_type="image/jpeg", caption="pic")
    loc = LocationContent(latitude=19.4, longitude=-99.1, name="CDMX")
    contact = ContactContent(name="Ana", phones=["+521"])
    reaction = ReactionContent(emoji="👍", reacted_message_id="wamid.0")
    msg = InboundMessage(
        id="wamid.2",
        sender="+521",
        timestamp=_TS,
        type=MessageType.IMAGE,
        media=media,
        location=loc,
        contacts=[contact],
        reaction=reaction,
        reply_to="wamid.0",
        forwarded=True,
        raw={"key": "val"},
    )
    assert msg.media.caption == "pic"
    assert msg.location.name == "CDMX"
    assert msg.contacts[0].name == "Ana"
    assert msg.reaction.emoji == "👍"
    assert msg.reply_to == "wamid.0"
    assert msg.forwarded is True
    assert msg.raw == {"key": "val"}


# ── OutboundMessage ───────────────────────────────────────────────────────────


def test_outbound_message_defaults() -> None:
    msg = OutboundMessage(type="text")
    assert msg.payload == {}


def test_outbound_message_with_payload() -> None:
    msg = OutboundMessage(
        type="interactive", payload={"interactive": {"type": "button"}}
    )
    assert msg.payload["interactive"]["type"] == "button"


# ── Content types ─────────────────────────────────────────────────────────────


def test_media_content_optional_fields() -> None:
    m = MediaContent(id="x", mime_type="audio/ogg")
    assert m.sha256 is None
    assert m.caption is None
    assert m.filename is None
    assert m.url is None


def test_location_content_optional_fields() -> None:
    loc = LocationContent(latitude=0.0, longitude=0.0)
    assert loc.name is None
    assert loc.address is None


def test_reaction_content() -> None:
    r = ReactionContent(emoji="❤️", reacted_message_id="wamid.x")
    assert r.emoji == "❤️"
    assert r.reacted_message_id == "wamid.x"
