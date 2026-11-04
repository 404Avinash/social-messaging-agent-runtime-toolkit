# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""personal-assistant's voice_reply speaks the user's language.

The wrapper called the recipe's ``text_to_voice(text)`` with no language, so
every voice note used the es-MX voice, English text included. It now takes a
``language`` limited to the voices the recipe has, and passes it through.
"""

from __future__ import annotations

import pytest

from tests.test_blueprint_scheduler_tools import _run_blueprint

pytest.importorskip("strands")

_DRIVER = r"""
import asyncio
import importlib
import json

from src import tools as t

# The package's voice_reply attribute is the tool, which shadows the module.
recipe = importlib.import_module("src.tools.voice_reply")

synthesized = []
sent = []


async def fake_text_to_voice(text, language="es-MX", voice_id=""):
    synthesized.append({"text": text, "language": language})
    return {"audio_id": "media-1"}


async def fake_send_message(phone, message, **kwargs):
    sent.append(phone)


t._text_to_voice = fake_text_to_voice
t.notify.send_message = fake_send_message

ctx = type("Ctx", (), {"invocation_state": {"phone": "+15551234567"}})()
results = {
    "english": asyncio.run(t.voice_reply(text="Good morning", tool_context=ctx, language="en-US")),
    "default": asyncio.run(t.voice_reply(text="Buenos días", tool_context=ctx)),
    "french": asyncio.run(t.voice_reply(text="Bonjour", tool_context=ctx, language="fr-FR")),
}
schema = t.voice_reply.tool_spec["inputSchema"]["json"]["properties"]["language"]
print(json.dumps({
    "results": results,
    "synthesized": synthesized,
    "sent": sent,
    "enum": schema.get("enum"),
    "voices": list(recipe._DEFAULT_VOICES),
}))
"""


def test_voice_reply_passes_the_language_through(tmp_path):
    out = _run_blueprint(tmp_path, "personal-assistant", _DRIVER)

    assert out["synthesized"] == [
        {"text": "Good morning", "language": "en-US"},
        {"text": "Buenos días", "language": "es-MX"},
    ]
    assert out["results"]["english"] == {"sent": True}
    assert out["results"]["default"] == {"sent": True}
    # A language with no voice is refused before Polly is called.
    assert out["results"]["french"]["status"] == "error"
    assert "en-US" in out["results"]["french"]["message"]
    assert out["sent"] == ["+15551234567", "+15551234567"]
    # The model is offered exactly the languages the recipe has a voice for.
    assert out["enum"] == out["voices"]
