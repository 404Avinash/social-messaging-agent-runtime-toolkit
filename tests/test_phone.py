# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Phone utilities: normalize_phone, actor_id_for_phone, session_id_for_phone, mask_phone.
"""

import pytest

from socialagent.utils.phone import (
    actor_id_for_phone,
    mask_phone,
    normalize_phone,
    session_id_for_phone,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+525555550100", "+525555550100"),
        ("525555550100", "+525555550100"),
        ("+52 555 555 0100", "+525555550100"),
        ("52-555-555-0100", "+525555550100"),
        ("(52) 555 555 0100", "+525555550100"),
        # Mexico mobile: the extra 1 after 52 must be stripped so that both
        # WhatsApp formats collapse to the same E.164 value.
        ("+5215555550100", "+525555550100"),
        ("5215555550100", "+525555550100"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+525555550100", "525555550100"),
        ("525555550100", "525555550100"),
        ("+1 800 555 0100", "18005550100"),
        # Same-number, two WhatsApp formats, identical actor ID.
        ("+5215555550100", "525555550100"),
        ("+525555550100", "525555550100"),
    ],
)
def test_actor_id_for_phone(phone: str, expected: str) -> None:
    assert actor_id_for_phone(phone) == expected


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+525555550100", "whatsapp-session-conversation-525555550100"),
        ("525555550100", "whatsapp-session-conversation-525555550100"),
    ],
)
def test_session_id_for_phone(phone: str, expected: str) -> None:
    assert session_id_for_phone(phone) == expected


def test_session_id_prefix() -> None:
    assert session_id_for_phone("+521").startswith("whatsapp-session-conversation-")


def test_actor_and_session_consistent() -> None:
    phone = "+525555550100"
    assert (
        session_id_for_phone(phone)
        == f"whatsapp-session-conversation-{actor_id_for_phone(phone)}"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+525555550100", "+...0100"),
        ("525555550100", "...0100"),
        ("+15555550100", "+...0100"),
        # However the number is written, only its last four digits remain.
        ("+52 55 5555 0100", "+...0100"),
        ("(555) 555-0100", "...0100"),
        # Ids built from the digits keep their shape, with the digits cut.
        (
            "whatsapp-session-conversation-525555550100",
            "whatsapp-session-conversation-...0100",
        ),
        ("sa-dev-525555550100-reminder-ab12", "sa-dev-...0100-reminder-ab12"),
        # Too short to be a number: left as it is.
        ("1234", "1234"),
        ("?", "?"),
        ("", ""),
        # A log helper must never raise, whatever it is handed.
        (None, ""),
        (525555550100, "...0100"),
    ],
)
def test_mask_phone(value: object, expected: str) -> None:
    assert mask_phone(value) == expected
