# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The appointment-scheduler's business-side cancellation finds the booking.

The agent stores the booking and names the reminder after the sender's number
as the webhook normalized it (``+52...``, Mexico's legacy mobile ``1`` dropped).
A business system calling the event API may send ``+521...`` or no ``+``; the
handler must look the booking up under the same key, or it finds nothing, the
booking stays and the reminder still fires.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STORED = "+525555550100"


def _visit_cancelled_handler(monkeypatch):
    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
    }.items():
        monkeypatch.setenv(k, v)
    # No __pycache__ in the blueprint, which `socialagent init` copies.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    handlers_dir = REPO_ROOT / "blueprints" / "appointment-scheduler" / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_event_probe", handlers_dir / "event.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)
    return mod.app._event_handlers["visit_cancelled"]


class FakeState:
    def __init__(self):
        self.items = {(STORED, "booking"): {"property": "Downtown Loft"}}

    async def get(self, phone, key):
        return self.items.get((phone, key))

    async def delete(self, phone, key):
        self.items.pop((phone, key), None)


class FakeCtx:
    def __init__(self):
        self.state = FakeState()
        self.cancelled: list[str] = []
        self.templates: list[str] = []
        self.memory: list[str] = []

    async def cancel_schedule(self, schedule_id):
        self.cancelled.append(schedule_id)

    async def write_to_memory(self, phone, text):
        self.memory.append(phone)

    async def send_template(self, phone, name, params, language="en"):
        self.templates.append(phone)


@pytest.mark.parametrize("sent", [STORED, "+5215555550100", "525555550100"])
def test_a_business_cancellation_finds_the_booking_however_the_number_is_written(
    monkeypatch, sent
):
    handler = _visit_cancelled_handler(monkeypatch)
    ctx = FakeCtx()
    asyncio.run(handler({"phone": sent, "property": "Downtown Loft"}, ctx))
    assert (STORED, "booking") not in ctx.state.items
    assert ctx.cancelled == ["reminder-525555550100"]
    assert ctx.memory == [STORED] and ctx.templates == [STORED]
