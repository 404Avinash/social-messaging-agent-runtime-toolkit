# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""_install_session_read_guard wraps the session manager's reads exactly once."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from bedrock_agentcore.memory.integrations.strands import (
    session_manager as session_manager_mod,
)

import socialagent_runtime

# How long one thread waits for the other. Bounds the test when the lock keeps
# the other thread out, which is the outcome the test wants.
_WAIT = 1.0


def test_concurrent_installs_wrap_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thread B passes the guard check, A installs completely, then B resumes.

    Without serialisation B wraps A's wrapper, so a read_agent that finds no
    state (every new session) retries twice and reads the original 4 times.
    """
    b_checked = threading.Event()
    a_done = threading.Event()
    agent_reads = []

    def read_session(self, session_id, **kwargs):
        return None

    def read_agent(self, session_id, agent_id, **kwargs):
        agent_reads.append(agent_id)
        return None

    class PausingReadSession:
        """read_session that parks thread B on its first lookup, after the check."""

        def __get__(self, obj, owner):
            if threading.current_thread().name == "B" and not b_checked.is_set():
                b_checked.set()
                a_done.wait(_WAIT)
            return read_session

    class FakeAgentCoreMemorySessionManager:
        read_session = PausingReadSession()

    FakeAgentCoreMemorySessionManager.read_agent = read_agent
    monkeypatch.setattr(
        session_manager_mod,
        "AgentCoreMemorySessionManager",
        FakeAgentCoreMemorySessionManager,
    )
    monkeypatch.setattr(socialagent_runtime, "_session_read_guard_installed", False)

    def install_a():
        b_checked.wait(_WAIT)
        socialagent_runtime._install_session_read_guard()
        a_done.set()

    threads = [
        threading.Thread(
            target=socialagent_runtime._install_session_read_guard, name="B"
        ),
        threading.Thread(target=install_a, name="A"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5 * _WAIT)

    manager = FakeAgentCoreMemorySessionManager()
    manager.memory_client = SimpleNamespace(list_events=lambda **kw: [])
    manager.config = SimpleNamespace(actor_id="actor-1")

    assert manager.read_agent("session-1", "agent-1") is None
    assert len(agent_reads) == 2, f"read_agent wrapped more than once: {agent_reads}"
