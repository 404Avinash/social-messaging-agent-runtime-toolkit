# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Phone number normalization and ID derivation for socialagent-runtime.
"""

import re


def normalize_phone(phone: str) -> str:
    """Return the phone number in E.164 format (``+<digits>``).

    Applies carrier-specific quirks:
    - Mexico (country code 52): strips the extra ``1`` that WhatsApp sometimes
      adds after the country code for mobile numbers. Both ``+525555550100``
      and ``+5215555550100`` normalize to ``+525555550100``.

    Example: ``"52 555 555 0100"`` → ``"+525555550100"``
    """
    digits = re.sub(r"\D", "", phone)
    # Mexico: drop the 1 after 52 when the full number is 13 digits.
    # Keeps legitimate 12-digit MX numbers intact (52 + 10 digit local).
    if digits.startswith("521") and len(digits) == 13:
        digits = "52" + digits[3:]
    return "+" + digits


def actor_id_for_phone(phone: str) -> str:
    """Return the AgentCore Memory actor ID for a phone number (digits only).

    Applies the same normalization as ``normalize_phone`` so the actor ID
    is stable regardless of which format WhatsApp delivers.

    Example: ``"+525555550100"`` → ``"525555550100"``
    """
    return normalize_phone(phone).lstrip("+")


def session_id_for_phone(phone: str) -> str:
    """Return the AgentCore Memory session ID for a phone number.

    Example: ``"+525555550100"`` → ``"whatsapp-session-conversation-525555550100"``
    """
    return "whatsapp-session-conversation-" + actor_id_for_phone(phone)


# A value that is nothing but a phone number, in any common format.
_PHONE_SHAPED = re.compile(r"\+?[\d\s().-]*")
# A run of 5+ digits: a phone number inside an id built from one.
_DIGIT_RUN = re.compile(r"\d{5,}")


def mask_phone(value: str | None) -> str:
    """Return ``value`` for a log line, each phone number in it cut to its last four digits.

    Log a user by this, never by the full number. CloudWatch Logs data
    protection masks phone numbers only for the countries in
    ``pii_phone_number_countries`` (it has no identifier for Mexico, ``+52``),
    and only in the Lambda log groups, not the agent runtime's, so a full
    number in a log line can stay readable.

    A value that is only a phone number, however it is formatted, keeps its
    ``+`` and its last four digits. In anything else (a session or actor id, a
    schedule name) each run of five or more digits is cut to its last four.
    Never raises, so it is safe inside a logging call.

    The Lambda Control Layer (``socialagent.utils.phone``) and the Agent Runtime
    Package (``socialagent_runtime.phone``) each keep this same function, because
    the two layers never import each other; a test keeps the copies identical.

    Example: ``"+525555550100"`` → ``"+...0100"``
    Example: ``"+52 55 5555 0100"`` → ``"+...0100"``
    Example: ``"whatsapp-session-conversation-525555550100"`` →
    ``"whatsapp-session-conversation-...0100"``
    """
    text = "" if value is None else str(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 5 and _PHONE_SHAPED.fullmatch(text):
        return ("+" if text.startswith("+") else "") + "..." + digits[-4:]
    return _DIGIT_RUN.sub(lambda m: "..." + m.group()[-4:], text)
