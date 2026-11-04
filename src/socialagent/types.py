# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Core data types for SMART: inbound/outbound message models and enumerations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MessageType(Enum):
    """Supported WhatsApp message types."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    LOCATION = "location"
    CONTACT = "contact"
    REACTION = "reaction"
    BUTTON_REPLY = "button_reply"
    LIST_REPLY = "list_reply"
    FORM_REPLY = "form_reply"
    STICKER = "sticker"


@dataclass
class MediaContent:
    """Media attachment metadata from an inbound message."""

    id: str
    mime_type: str
    sha256: str | None = None
    caption: str | None = None
    filename: str | None = None
    url: str | None = None  # Populated after download


@dataclass
class LocationContent:
    """Geographic location shared in a message."""

    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


@dataclass
class ContactContent:
    """Contact card shared in a message."""

    name: str
    phones: list[str]


@dataclass
class ReactionContent:
    """Emoji reaction to a previous message."""

    emoji: str
    reacted_message_id: str


@dataclass
class StatusUpdate:
    """Delivery/read status update for a message sent by SMART.

    Received via the same SNS webhook as inbound messages when WhatsApp
    reports that a sent message was delivered, read, or failed.
    """

    message_id: str  # wamid of the sent message
    recipient: str  # E.164 phone number of the recipient
    status: str  # "sent" | "delivered" | "read" | "failed"
    timestamp: datetime
    raw: dict = field(default_factory=dict)


@dataclass
class InboundMessage:
    """Normalized representation of any inbound WhatsApp message."""

    id: str
    sender: str  # E.164 normalized, e.g. "+525555550100"
    timestamp: datetime
    type: MessageType
    text: str | None = None
    media: MediaContent | None = None
    location: LocationContent | None = None
    contacts: list[ContactContent] | None = None
    reaction: ReactionContent | None = None
    reply_to: str | None = (
        None  # wamid of the message being replied to (from context.id)
    )
    forwarded: bool = False
    raw: dict = field(default_factory=dict)  # Original provider payload


@dataclass
class OutboundMessage:
    """Provider-agnostic outbound message container.

    ``type`` maps to the WhatsApp API message type (e.g. ``"text"``,
    ``"interactive"``, ``"template"``, ``"image"``).  ``payload`` holds the
    type-specific fields merged into the final API request body.
    """

    type: str
    payload: dict = field(default_factory=dict)
