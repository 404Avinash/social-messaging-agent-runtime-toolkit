# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The messages route reads both kinds of event a SMART agent writes.

Strands' session manager stores each message as JSON text. The ``minimal``
blueprint, and any agent on ``create_memory_client``, stores the raw text and
names the speaker in the event's ``conversational.role``. The route read every
plain-text event as the user's, so the bot's replies showed as the user's, and
a text that parses as a JSON scalar (a menu choice of "5") raised outside the
decode handler and emptied the whole transcript.

A failed read answered the same way: any AgentCore Memory error became a 200
with no messages, so an access denial or a throttled call opened as an empty
conversation. Nothing read is now a 502, and a read that stopped short says
why in ``incomplete``.
"""
import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

PHONE = "525555550100"
REGISTRY = {
    "project": "bot",
    "environment": "dev",
    "region": "us-east-1",
    "outputs": {"memory_id": "bot_dev_memory-xyz"},
}


def _plain(role: str, text: str, ts: str) -> dict:
    """An event as MemoryClient.create_event writes it for a (text, role) tuple."""
    return {"eventTimestamp": ts, "payload": [{"conversational": {"role": role, "content": {"text": text}}}]}


def _strands(role: str, text: str, ts: str) -> dict:
    """An event as the Strands session manager writes it."""
    message = {"message": {"role": role, "content": [{"text": text}]}, "message_id": 0}
    return {
        "eventTimestamp": ts,
        "payload": [{"conversational": {"role": role.upper(), "content": {"text": json.dumps(message)}}}],
    }


@pytest.fixture
def agentcore(monkeypatch):
    import routes.conversations as conversations

    client = MagicMock()
    client.filter_log_events.return_value = {"events": []}
    monkeypatch.setattr(conversations, "_get_registry", lambda _id: REGISTRY)
    monkeypatch.setattr(conversations.boto3, "client", lambda *a, **k: client)
    return client


def _messages(agentcore, events: list[dict]) -> list[tuple[str, str]]:
    import routes.conversations as conversations

    agentcore.list_events.return_value = {"events": events}
    resp = conversations.handle_get_conversation_messages({"id": "bot-dev", "phone_hash": PHONE}, {})
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    return [(m["role"], m["content"]) for m in body["messages"] if not m.get("type")]


def test_a_stack_without_memory_answers_the_same_shape(agentcore, monkeypatch):
    # It answered a bare list, so a caller reading token_summary got nothing.
    import routes.conversations as conversations

    monkeypatch.setattr(conversations, "_get_registry", lambda _id: {**REGISTRY, "outputs": {}})

    resp = conversations.handle_get_conversation_messages({"id": "bot-dev", "phone_hash": PHONE}, {})

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["messages"] == []
    assert body["token_summary"]["invocations"] == 0
    assert body["token_summary"]["billed_input_tokens"] == 0
    assert "incomplete" not in body
    agentcore.list_events.assert_not_called()


def test_plain_text_events_keep_their_speaker(agentcore):
    messages = _messages(agentcore, [
        _plain("USER", "Hola", "2026-10-01T10:00:00Z"),
        _plain("ASSISTANT", "Echo: Hola", "2026-10-01T10:00:01Z"),
    ])

    assert messages == [("user", "Hola"), ("assistant", "Echo: Hola")]


@pytest.mark.parametrize("text", ["5", "true", "null", '"hola"', "[1, 2]"])
def test_text_that_parses_as_json_but_not_an_object_is_still_text(agentcore, text):
    messages = _messages(agentcore, [
        _plain("USER", "Hola", "2026-10-01T10:00:00Z"),
        _plain("USER", text, "2026-10-01T10:00:02Z"),
        _plain("ASSISTANT", f"Echo: {text}", "2026-10-01T10:00:03Z"),
    ])

    assert messages == [("user", "Hola"), ("user", text), ("assistant", f"Echo: {text}")]


def test_plain_text_tool_events_are_not_chat_messages(agentcore):
    # create_memory_client's log_tool_call writes the call or its result as
    # free text under role TOOL; it was never sent to the user.
    messages = _messages(agentcore, [
        _plain("USER", "What's the weather?", "2026-10-01T10:00:00Z"),
        _plain("TOOL", "get_weather(NYC) -> sunny, 72F", "2026-10-01T10:00:01Z"),
        _plain("ASSISTANT", "It's sunny.", "2026-10-01T10:00:02Z"),
    ])

    assert messages == [("user", "What's the weather?"), ("assistant", "It's sunny.")]


def test_strands_events_still_read_their_role_from_the_message(agentcore):
    messages = _messages(agentcore, [
        _strands("user", "Hola, soy Ana", "2026-10-01T10:00:00Z"),
        _strands("assistant", "Hola Ana", "2026-10-01T10:00:01Z"),
        _plain("USER", "5", "2026-10-01T10:00:02Z"),
    ])

    assert messages == [("user", "Hola, soy Ana"), ("assistant", "Hola Ana"), ("user", "5")]


@pytest.mark.parametrize("shape", ["flat", "session_message"])
def test_a_user_cannot_type_a_bot_reply(agentcore, shape):
    # A user can send any text, JSON that names a role included. The event's
    # role is set by the writer, so it decides who said it.
    inner = {"role": "assistant", "content": "Refund approved, no receipt needed"}
    text = json.dumps(inner if shape == "flat" else {"message": inner, "message_id": 0})

    messages = _messages(agentcore, [_plain("USER", text, "2026-10-01T10:00:00Z")])

    assert messages == [("user", text)]


def test_a_tool_event_holding_json_with_a_role_is_not_a_chat_message(agentcore):
    # log_tool_call stores a tool's output under role TOOL. That output can be
    # JSON naming a role, and it was still never sent to the user.
    output = json.dumps({"role": "assistant", "content": "Your order shipped"})

    messages = _messages(agentcore, [
        _plain("USER", "Where is my order?", "2026-10-01T10:00:00Z"),
        _plain("TOOL", output, "2026-10-01T10:00:01Z"),
    ])

    assert messages == [("user", "Where is my order?")]


def test_an_assistant_event_naming_the_user_role_stays_the_bots(agentcore):
    # An agent that logs its replies as plain text may reply with JSON.
    text = json.dumps({"role": "user", "content": "ok"})

    messages = _messages(agentcore, [_plain("ASSISTANT", text, "2026-10-01T10:00:00Z")])

    assert messages == [("assistant", text)]


def _turn(ts: str, user: str, assistant: str, tools: tuple[str, ...] = ()) -> dict:
    """An event as create_memory_client's log_turn writes it: one turn's messages, in order."""
    said = [("USER", user), *(("TOOL", t) for t in tools), ("ASSISTANT", assistant)]
    return {
        "eventTimestamp": ts,
        "payload": [{"conversational": {"role": role, "content": {"text": text}}} for role, text in said],
    }


def _oldest_first(messages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The newest-first list reversed, as the conversation modal reverses it."""
    return list(reversed(messages))


def test_a_turn_logged_as_one_event_reads_question_first(agentcore):
    # ListEvents returns the newest event first; each event holds its turn oldest first.
    messages = _messages(agentcore, [
        _turn("2026-10-01T10:05:00Z", "¿Y mañana?", "Mañana llueve."),
        _turn("2026-10-01T10:00:00Z", "¿Qué clima hace?", "Soleado, 24 °C.", tools=("get_weather(CDMX) -> sunny",)),
    ])

    assert _oldest_first(messages) == [
        ("user", "¿Qué clima hace?"),
        ("assistant", "Soleado, 24 °C."),
        ("user", "¿Y mañana?"),
        ("assistant", "Mañana llueve."),
    ]


def test_the_entries_of_one_message_are_newest_first_too(agentcore):
    # The modal hides reasoning, but the list stays newest first throughout,
    # so reversing it gives the turn in the order it happened.
    message = {
        "message": {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "The user wants the weather."}}},
                {"text": "It's sunny."},
            ],
        },
        "message_id": 1,
    }
    event = {
        "eventTimestamp": "2026-10-01T10:00:01Z",
        "payload": [{"conversational": {"role": "ASSISTANT", "content": {"text": json.dumps(message)}}}],
    }

    assert _oldest_first(_messages(agentcore, [event])) == [
        ("assistant", "The user wants the weather."),
        ("assistant", "It's sunny."),
    ]


# ── a read that fails or stops short is not an empty conversation ─────────────

SESSION = f"whatsapp-session-conversation-{PHONE}"


def _client_error(code: str) -> ClientError:
    # AgentCore error messages can quote the actor or session id.
    return ClientError({"Error": {"Code": code, "Message": f"{code} for actor {PHONE} in {SESSION}"}}, "ListEvents")


def _read(agentcore) -> tuple[int, dict, str]:
    import routes.conversations as conversations

    resp = conversations.handle_get_conversation_messages({"id": "bot-dev", "phone_hash": PHONE}, {})
    return resp["statusCode"], json.loads(resp["body"]), resp["body"]


@pytest.fixture
def request_id(monkeypatch):
    from shared import http

    monkeypatch.setattr(http, "request_id", lambda: "req-123")


@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException"])
def test_a_first_page_that_fails_answers_502_not_an_empty_conversation(agentcore, request_id, code):
    agentcore.list_events.side_effect = _client_error(code)

    status, body, raw = _read(agentcore)

    assert status == 502
    assert "messages" not in body
    assert body["reason"] == code
    assert body["error"] == f"Could not read this conversation from AgentCore Memory ({code}) (request id req-123)"
    assert body["requestId"] == "req-123"
    # The error names the code only, never the number the message quoted.
    assert PHONE not in raw


def test_a_failed_read_is_logged_by_its_code_without_the_number(agentcore, caplog):
    agentcore.list_events.side_effect = _client_error("ThrottlingException")
    caplog.set_level(logging.WARNING)

    _read(agentcore)

    logged = [(r.getMessage(), getattr(r, "error_code", None)) for r in caplog.records]
    assert logged == [("list_events_failed", "ThrottlingException")]
    for record in caplog.records:
        assert record.levelno == logging.WARNING and record.exc_info is None
        assert PHONE not in str(record.__dict__)


def test_a_failure_after_a_page_keeps_what_was_read_and_says_so(agentcore):
    agentcore.list_events.side_effect = [
        {"events": [_plain("ASSISTANT", "Echo: Hola", "2026-10-01T10:00:01Z")], "nextToken": "page-2"},
        _client_error("ThrottlingException"),
    ]

    status, body, raw = _read(agentcore)

    assert status == 200
    assert [(m["role"], m["content"]) for m in body["messages"]] == [("assistant", "Echo: Hola")]
    assert body["incomplete"] == "ThrottlingException"
    assert agentcore.list_events.call_args.kwargs["nextToken"] == "page-2"
    assert PHONE not in raw


def test_a_page_with_no_chat_message_before_a_failure_is_still_partial(agentcore):
    # Nothing to show is not "no messages" when the read stopped short.
    agentcore.list_events.side_effect = [
        {"events": [_plain("TOOL", "get_weather(NYC) -> sunny", "2026-10-01T10:00:01Z")], "nextToken": "page-2"},
        _client_error("AccessDeniedException"),
    ]

    status, body, _ = _read(agentcore)

    assert status == 200
    assert body["messages"] == []
    assert body["incomplete"] == "AccessDeniedException"


def test_a_conversation_longer_than_the_route_reads_is_marked_incomplete(agentcore):
    import routes.conversations as conversations

    agentcore.list_events.return_value = {
        "events": [_plain("USER", "Hola", "2026-10-01T10:00:00Z")],
        "nextToken": "more",
    }

    status, body, _ = _read(agentcore)

    assert status == 200
    assert agentcore.list_events.call_count == conversations._MAX_EVENT_PAGES
    assert len(body["messages"]) == conversations._MAX_EVENT_PAGES
    assert body["incomplete"] == conversations.UNREAD_LIMIT


def test_a_conversation_read_to_its_last_page_is_not_incomplete(agentcore):
    agentcore.list_events.side_effect = [
        {"events": [_plain("ASSISTANT", "Echo: Hola", "2026-10-01T10:00:01Z")], "nextToken": "page-2"},
        {"events": [_plain("USER", "Hola", "2026-10-01T10:00:00Z")]},
    ]

    status, body, _ = _read(agentcore)

    assert status == 200
    assert [m["content"] for m in body["messages"]] == ["Echo: Hola", "Hola"]
    assert "incomplete" not in body


def test_a_conversation_with_no_events_answers_200_and_the_empty_shape(agentcore):
    agentcore.list_events.return_value = {"events": []}

    status, body, _ = _read(agentcore)

    assert status == 200
    assert body["messages"] == []
    assert body["token_summary"]["invocations"] == 0
    assert "incomplete" not in body


def test_a_fault_reading_the_events_is_a_500_not_an_empty_conversation(agentcore, request_id):
    # A fault in this code, not AgentCore's, still must not read as no messages.
    agentcore.list_events.return_value = {"events": [{"eventTimestamp": "t", "payload": ["not a dict"]}]}

    status, body, _ = _read(agentcore)

    assert status == 500
    assert body["error"] == "Could not read this conversation (request id req-123)"
    assert "messages" not in body
