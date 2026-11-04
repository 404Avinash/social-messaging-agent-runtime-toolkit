# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Replay-safe restored session history."""

from __future__ import annotations

import tempfile

import pytest
from strands.session.file_session_manager import FileSessionManager
from strands.types.exceptions import SessionException
from strands.types.session import SessionMessage

import socialagent_runtime
from socialagent_runtime._history import strip_replay_unsafe


def _file_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FileSessionManager:
    """The dev-mode session manager create_session_manager returns for session-1."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return socialagent_runtime.create_session_manager(
        {
            "socialagent": {
                "version": 1,
                "memory_id": "__socialagent_dev__",
                "session_id": "session-1",
                "actor_id": "actor-1",
            }
        }
    )


def _message(
    content: list[dict], message_id: int = 0, **message_fields
) -> SessionMessage:
    return SessionMessage(
        message={"role": "assistant", "content": content, **message_fields},
        message_id=message_id,
        created_at=f"created-{message_id}",
        updated_at=f"updated-{message_id}",
    )


def test_policy_is_noop_without_context_bound_blocks() -> None:
    original = _message([{"text": "hello"}, {"unknownFutureBlock": {}}])

    restored = strip_replay_unsafe([original])

    assert restored == [original]
    assert restored[0] is original


def test_policy_drops_reasoning_and_messages_it_empties() -> None:
    reasoning = {"reasoningContent": {"reasoningText": {"text": "private"}}}
    mixed = _message([reasoning, {"text": "answer"}], message_id=1)
    reasoning_only = _message([reasoning], message_id=2)

    restored = strip_replay_unsafe([mixed, reasoning_only])

    assert [message.to_message() for message in restored] == [
        {"role": "assistant", "content": [{"text": "answer"}]}
    ]
    assert restored[0].message_id == 1
    assert restored[0].created_at == "created-1"
    assert restored[0].updated_at == "updated-1"


def test_policy_filters_effective_redaction_and_preserves_message_fields() -> None:
    reasoning = {"reasoningContent": {"reasoningText": {"text": "private"}}}
    original = _message([{"text": "unredacted"}], message_id=7, tracking_id="track-7")
    original.redact_message = {
        "role": "assistant",
        "content": [reasoning, {"text": "redacted answer"}],
        "tracking_id": "redacted-track",
    }

    (restored,) = strip_replay_unsafe([original])

    assert restored.to_message() == {
        "role": "assistant",
        "content": [{"text": "redacted answer"}],
        "tracking_id": "redacted-track",
    }
    assert restored.redact_message is None
    assert restored.message_id == 7
    assert restored.created_at == "created-7"
    assert restored.updated_at == "updated-7"


def test_file_manager_filters_before_pagination_without_rewriting_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = _file_manager(monkeypatch, tmp_path)
    reasoning = {"reasoningContent": {"reasoningText": {"text": "private"}}}
    history = [
        _message([reasoning], message_id=0),
        _message([{"text": "first"}], message_id=1),
        _message([{"text": "second"}], message_id=2, role="user"),
    ]
    for message in history:
        manager.create_message("session-1", "agent-1", message)

    restored_without_limit = manager.list_messages("session-1", "agent-1", offset=1)
    restored_finite_page = manager.list_messages(
        "session-1", "agent-1", limit=1, offset=1
    )
    restored_first_page = manager.list_messages(
        "session-1", "agent-1", limit=2, offset=0
    )
    persisted = FileSessionManager(
        session_id="session-1", storage_dir=manager.storage_dir
    ).list_messages("session-1", "agent-1")

    expected_second = [{"role": "user", "content": [{"text": "second"}]}]
    assert [
        message.to_message() for message in restored_without_limit
    ] == expected_second
    assert [message.to_message() for message in restored_finite_page] == expected_second
    assert [message.to_message() for message in restored_first_page] == [
        {"role": "assistant", "content": [{"text": "first"}]},
        {"role": "user", "content": [{"text": "second"}]},
    ]
    assert any(
        "reasoningContent" in block
        for message in persisted
        for block in message.to_message()["content"]
    )

    with pytest.raises(SessionException):
        manager.list_messages("missing-session", "agent-1", limit=0)


def test_file_manager_strips_tool_exchange_like_the_deployed_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Dev mode restores exactly as AgentCore Memory does, so socialagent dev
    reproduces what the deployed stack will do with the same history."""
    manager = _file_manager(monkeypatch, tmp_path)
    reasoning = {"reasoningContent": {"reasoningText": {"text": "private"}}}
    history = [
        _message([{"text": "hola"}], 0, role="user"),
        SessionMessage(
            message={
                "role": "assistant",
                "content": [
                    reasoning,
                    {
                        "toolUse": {
                            "toolUseId": "tool-1",
                            "name": "attach_interactive",
                            "input": {"message_type": "buttons", "body": "¿Cuál?"},
                        }
                    },
                ],
            },
            message_id=1,
        ),
        SessionMessage(
            message={
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tool-1",
                            "content": [{"text": "done"}],
                        }
                    }
                ],
            },
            message_id=2,
        ),
    ]
    for message in history:
        manager.create_message("session-1", "agent-1", message)

    restored = [
        message.to_message()
        for message in manager.list_messages("session-1", "agent-1")
    ]

    assert restored == [
        {"role": "user", "content": [{"text": "hola"}]},
        {"role": "assistant", "content": [{"text": "¿Cuál?"}]},
    ]


def test_create_session_manager_uses_replay_safe_file_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = _file_manager(monkeypatch, tmp_path)

    assert isinstance(manager, FileSessionManager)
    assert type(manager).__name__ == "ReplaySafeFileSessionManager"


def test_agentcore_manager_filters_before_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bedrock_agentcore.memory.integrations.strands import (
        session_manager as session_manager_mod,
    )

    reasoning = {"reasoningContent": {"reasoningText": {"text": "private"}}}
    stored = [
        _message([reasoning], message_id=0),
        _message([{"text": "first"}], message_id=1),
        _message([{"text": "second"}], message_id=2, role="user"),
    ]
    calls = []

    class FakeAgentCoreMemorySessionManager:
        def __init__(self, **kwargs):
            pass

        def list_messages(self, session_id, agent_id, limit=None, offset=0, **kwargs):
            calls.append((limit, offset))
            return stored

    monkeypatch.setattr(
        session_manager_mod,
        "AgentCoreMemorySessionManager",
        FakeAgentCoreMemorySessionManager,
    )
    monkeypatch.setattr(
        socialagent_runtime, "_install_session_read_guard", lambda: None
    )
    manager = socialagent_runtime.create_session_manager(
        {
            "socialagent": {
                "version": 1,
                "memory_id": "mem-1",
                "session_id": "session-1",
                "actor_id": "actor-1",
            }
        }
    )

    page = manager.list_messages("session-1", "agent-1", limit=1, offset=1)

    assert [message.message_id for message in page] == [2]
    assert calls == [(None, 0)]


def test_image_and_document_blocks_are_stripped_but_their_text_stays():
    """An agent called with attachments.build_prompt persists the bytes with the
    user turn; on restore the file must not replay, the description must."""
    from socialagent_runtime._history import REPLAY_UNSAFE_BLOCKS, strip_replay_unsafe

    assert {"image", "document"} <= REPLAY_UNSAFE_BLOCKS
    msg = _message(
        [
            {"text": "[User sent an image: a receipt]"},
            {"image": {"format": "jpeg", "source": {"bytes": b"\xff\xd8"}}},
            {
                "document": {
                    "format": "pdf",
                    "name": "invoice",
                    "source": {"bytes": b"%PDF"},
                }
            },
        ],
        role="user",
    )
    out = strip_replay_unsafe([msg])
    assert [b for m in out for b in m.to_message()["content"]] == [
        {"text": "[User sent an image: a receipt]"}
    ]


# --- Tool-only assistant turns -------------------------------------------------
#
# A bot that answers through tools (attach_interactive, send_text) produces
# assistant messages whose only blocks are reasoningContent and toolUse. Strip
# both and the message is empty; drop it and the restored history is the user
# talking to no one. The fixture is a real session of the appointment
# scheduler on a reasoning model: 24 assistant turns, 1 with a text block.


def _real_history() -> list[SessionMessage]:
    import json
    from pathlib import Path

    raw = json.loads(
        (
            Path(__file__).parent / "fixtures" / "restored_history_tool_heavy.json"
        ).read_text()
    )
    return [
        SessionMessage(message=m, message_id=i, created_at=f"c{i}", updated_at=f"u{i}")
        for i, m in enumerate(raw)
    ]


def test_tool_only_assistant_turn_is_kept_as_the_text_the_user_saw() -> None:
    body = "¿Cuál te gustaría ver?"
    turn = _message(
        [
            {"reasoningContent": {"reasoningText": {"text": "", "signature": "s"}}},
            {
                "toolUse": {
                    "toolUseId": "tu",
                    "name": "attach_interactive",
                    "input": {
                        "message_type": "buttons",
                        "body": body,
                        "options": "A,B",
                    },
                }
            },
        ]
    )
    result = _message(
        [
            {
                "toolResult": {
                    "toolUseId": "tu",
                    "status": "success",
                    "content": [{"text": "{}"}],
                }
            }
        ],
        1,
        role="user",
    )
    (kept,) = strip_replay_unsafe([turn, result])
    assert kept.message["role"] == "assistant"
    assert kept.message["content"] == [{"text": body}]


def test_restored_real_history_keeps_both_sides_and_alternates() -> None:
    kept = strip_replay_unsafe(_real_history())
    roles = [m.message["role"] for m in kept]
    # 23 stored assistant turns, none with a text block: before the fix the
    # restored history had no assistant message at all. Same-role runs (a
    # load_skill call then the reply, in one agent turn) merge, so 11 is the
    # whole assistant side of this conversation, one per user turn.
    assert roles.count("assistant") == 11, roles
    assert roles.count("user") == 11, roles
    assert all(a != b for a, b in zip(roles, roles[1:])), roles  # alternation
    assert roles[0] == "user"
    bodies = [b["text"] for m in kept for b in m.message["content"]]
    assert any("¿Cuál te gustaría ver?" in b for b in bodies)  # a menu the user saw
    for m in kept:
        for block in m.message["content"]:
            assert set(block) <= {"text"}, block  # nothing replay-unsafe survives
            assert block["text"].strip()


def test_internal_tool_turn_with_no_user_visible_text_drops_with_its_result() -> None:
    """load_skill / execute_action say nothing to the user; the pair drops
    together so the remaining history still alternates."""
    history = [
        _message([{"text": "hola"}], 0, role="user"),
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "tu",
                        "name": "load_skill",
                        "input": {"skill_name": "x"},
                    }
                }
            ],
            1,
        ),
        _message(
            [
                {
                    "toolResult": {
                        "toolUseId": "tu",
                        "status": "success",
                        "content": [{"text": "{}"}],
                    }
                }
            ],
            2,
            role="user",
        ),
        _message([{"text": "¿En qué te ayudo?"}], 3),
    ]
    kept = strip_replay_unsafe(history)
    assert [m.message["role"] for m in kept] == ["user", "assistant"]
    assert kept[1].message["content"] == [{"text": "¿En qué te ayudo?"}]


def test_file_manager_never_overwrites_a_stored_message_after_a_filtered_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The stored history ends on an internal tool exchange (a skill load), which
    the restore drops. Strands allocates the next id from the last *restored*
    message, so without a guard the next write lands on an existing file and
    destroys it. Ids come from the stored maximum instead."""
    manager = _file_manager(monkeypatch, tmp_path)
    stored = [
        _message([{"text": "hola"}], 0, role="user"),
        _message([{"text": "¿Cuál?"}], 1),
        _message([{"text": "sí"}], 2, role="user"),
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "t",
                        "name": "load_skill",
                        "input": {"skill_name": "x"},
                    }
                }
            ],
            3,
        ),
        _message(
            [
                {
                    "toolResult": {
                        "toolUseId": "t",
                        "status": "success",
                        "content": [{"text": "{}"}],
                    }
                }
            ],
            4,
            role="user",
        ),
    ]
    for m in stored:
        manager.create_message("session-1", "agent-1", m)
    restored = manager.list_messages("session-1", "agent-1")
    assert [m.message_id for m in restored] == [0, 1, 2]  # the tool exchange is gone

    # What Strands does next: id = last restored + 1 = 3, which exists on disk.
    manager.create_message("session-1", "agent-1", _message([{"text": "nuevo"}], 3))

    raw = FileSessionManager(
        session_id="session-1", storage_dir=manager.storage_dir
    ).list_messages("session-1", "agent-1")
    ids = [m.message_id for m in raw]
    assert ids == [0, 1, 2, 3, 4, 5], ids  # nothing lost; the new one got 5
    assert raw[3].to_message()["content"][0]["toolUse"]["name"] == "load_skill"
    assert raw[5].to_message()["content"] == [{"text": "nuevo"}]


def test_guardrail_redaction_after_a_filtered_restore_hits_the_new_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Strands remembers the message object it appended and redacts by that
    object's id. The reallocation must therefore change the id on that same
    object: a reallocated copy leaves Strands pointing at the old id, and a
    guardrail redaction then overwrites a stored message instead of the new one."""
    manager = _file_manager(monkeypatch, tmp_path)
    stored = [
        _message([{"text": "hola"}], 0, role="user"),
        _message([{"text": "¿Cuál?"}], 1),
        _message(
            [{"toolUse": {"toolUseId": "t", "name": "load_skill", "input": {}}}], 2
        ),
        _message(
            [{"toolResult": {"toolUseId": "t", "status": "success", "content": []}}],
            3,
            role="user",
        ),
    ]
    for m in stored:
        manager.create_message("session-1", "agent-1", m)
    restored = manager.list_messages("session-1", "agent-1")
    assert [m.message_id for m in restored] == [0, 1]
    # What initialize() leaves behind: the latest message is the last restored.
    manager._latest_agent_message["agent-1"] = restored[-1]

    class _Agent:
        agent_id = "agent-1"

    manager.append_message({"role": "user", "content": [{"text": "secreto"}]}, _Agent())
    manager.redact_latest_message(
        {"role": "user", "content": [{"text": "[redactado]"}]}, _Agent()
    )

    raw = FileSessionManager(
        session_id="session-1", storage_dir=manager.storage_dir
    ).list_messages("session-1", "agent-1")
    assert [m.message_id for m in raw] == [0, 1, 2, 3, 4]
    assert raw[2].redact_message is None  # the stored toolUse is untouched
    assert raw[2].to_message()["content"][0]["toolUse"]["name"] == "load_skill"
    assert raw[4].to_message()["content"] == [{"text": "[redactado]"}]


# --- A toolUse persists before the tool runs; only a delivered one counts ------
#
# Strands appends the assistant message (with its toolUse) before executing the
# tool. If the tool raises, or the turn raises after it and the webhook drops
# the queue, the toolUse is in Memory but the user received nothing. Standing
# in the tool's text would then claim the assistant said something it did not.


def test_tool_only_turn_whose_result_is_an_error_drops() -> None:
    history = [
        _message([{"text": "hola"}], 0, role="user"),
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "t",
                        "name": "attach_interactive",
                        "input": {"body": "¿Cuál?"},
                    }
                }
            ],
            1,
        ),
        _message(
            [
                {
                    "toolResult": {
                        "toolUseId": "t",
                        "status": "error",
                        "content": [{"text": "boom"}],
                    }
                }
            ],
            2,
            role="user",
        ),
    ]
    kept = strip_replay_unsafe(history)
    assert [m.message["role"] for m in kept] == ["user"], [m.message for m in kept]


def test_tool_only_turn_with_no_result_at_all_drops() -> None:
    """The turn raised before the result was written: the last stored message
    is the toolUse. Nothing was sent."""
    history = [
        _message([{"text": "hola"}], 0, role="user"),
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "t",
                        "name": "attach_interactive",
                        "input": {"body": "¿Cuál?"},
                    }
                }
            ],
            1,
        ),
    ]
    kept = strip_replay_unsafe(history)
    assert [m.message["role"] for m in kept] == ["user"]


def test_tool_only_turn_with_a_successful_result_is_kept_as_its_text() -> None:
    history = [
        _message([{"text": "hola"}], 0, role="user"),
        _message(
            [
                {
                    "toolUse": {
                        "toolUseId": "t",
                        "name": "attach_interactive",
                        "input": {"body": "¿Cuál?"},
                    }
                }
            ],
            1,
        ),
        _message(
            [
                {
                    "toolResult": {
                        "toolUseId": "t",
                        "status": "success",
                        "content": [{"text": "{}"}],
                    }
                }
            ],
            2,
            role="user",
        ),
    ]
    kept = strip_replay_unsafe(history)
    assert [m.message["content"] for m in kept] == [
        [{"text": "hola"}],
        [{"text": "¿Cuál?"}],
    ]
