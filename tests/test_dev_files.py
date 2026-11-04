# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dev-mode files are owner-only, and never land in a directory another user made.

``socialagent dev`` keeps the dev inbox, the dev state and the local conversation
sessions on disk, and the buffer store falls back to a file, all under the temp
dir, which every user on a shared Linux host can write. A predictable name with
the default umask left them readable by everyone; a directory another user
created first would have let that user read or plant them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import _devfiles as runtime_devfiles  # noqa: E402
import notify as notify_mod  # noqa: E402
import state as state_mod  # noqa: E402

from socialagent.buffer import store as buffer_store  # noqa: E402
from socialagent.utils import devfiles as control_devfiles  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

_posix_only = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="ownership checks need POSIX uids"
)

# Each dev file: (module, path attribute, write, read).
_STORES = [
    pytest.param(
        notify_mod,
        "_DEV_INBOX_PATH",
        lambda: notify_mod._dev_inbox_write([{"text": "hola"}]),
        notify_mod._dev_inbox_read_all,
        id="inbox",
    ),
    pytest.param(
        state_mod,
        "_DEV_STATE_PATH",
        lambda: state_mod._dev_write({"+15555550100#k": 1}),
        state_mod._dev_read,
        id="state",
    ),
    pytest.param(
        buffer_store,
        "_DEV_PATH",
        lambda: buffer_store._dev_write({"k": 1}),
        buffer_store._dev_read,
        id="buffer",
    ),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_both_layers_carry_the_same_helper() -> None:
    """The layers never import each other, so each keeps a copy; one fix, both copies."""
    runtime = REPO / "packages" / "socialagent-runtime" / "_devfiles.py"
    control = REPO / "src" / "socialagent" / "utils" / "devfiles.py"
    assert control.read_bytes() == runtime.read_bytes(), (
        "src/socialagent/utils/devfiles.py differs from "
        "packages/socialagent-runtime/_devfiles.py; copy one over the other"
    )


def test_every_dev_file_lives_in_the_per_user_dir() -> None:
    expected = runtime_devfiles.dev_dir()
    assert expected == control_devfiles.dev_dir()
    assert notify_mod._DEV_INBOX_PATH.parent == expected
    assert state_mod._DEV_STATE_PATH.parent == expected
    assert buffer_store._DEV_PATH.parent == expected


@pytest.mark.parametrize(("module", "attr", "write", "read"), _STORES)
def test_dev_files_are_owner_only_whatever_the_umask(
    monkeypatch, tmp_path, module, attr, write, read
):
    target = tmp_path / "socialagent-dev-test" / "file.json"
    monkeypatch.setattr(module, attr, target)
    old = os.umask(0)
    try:
        write()
    finally:
        os.umask(old)
    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600
    assert read()


def test_an_own_dir_with_wider_mode_is_narrowed(tmp_path):
    d = tmp_path / "socialagent-dev-test"
    d.mkdir()
    d.chmod(0o755)
    runtime_devfiles.private_dir(d)
    assert _mode(d) == 0o700


def test_an_existing_file_with_wider_mode_is_narrowed(tmp_path):
    d = runtime_devfiles.private_dir(tmp_path / "socialagent-dev-test")
    (d / "file.json").write_text("{}")
    (d / "file.json").chmod(0o644)
    runtime_devfiles.write_private(d / "file.json", '{"k": 1}')
    assert _mode(d / "file.json") == 0o600
    assert (d / "file.json").read_text() == '{"k": 1}'


@_posix_only
@pytest.mark.parametrize(("module", "attr", "write", "read"), _STORES)
def test_a_dir_owned_by_another_user_is_refused(
    monkeypatch, tmp_path, module, attr, write, read
):
    """Stands in for a directory another user created first under /tmp."""
    planted = tmp_path / "socialagent-dev-test"
    planted.mkdir(mode=0o777)
    (planted / "file.json").write_text('{"planted": true}')
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    monkeypatch.setattr(module, attr, planted / "file.json")
    assert not read()  # a planted value is never read
    with pytest.raises(PermissionError, match="not a directory owned by this user"):
        write()
    assert (planted / "file.json").read_text() == '{"planted": true}'


def _plant(tmp_path: Path, monkeypatch, planted_by: str) -> Path:
    """A dev dir this user may not use: a symlink, or one another user made."""
    planted = tmp_path / "socialagent-dev-test"
    if planted_by == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        planted.symlink_to(elsewhere, target_is_directory=True)
    else:
        planted.mkdir(mode=0o777)
        me = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: me + 1)
    return planted


@_posix_only
@pytest.mark.parametrize("planted_by", ["symlink", "another-user"])
@pytest.mark.parametrize(("module", "attr", "write", "read"), _STORES)
def test_a_refused_dir_is_logged_not_read_as_empty_silently(
    monkeypatch, tmp_path, caplog, planted_by, module, attr, write, read
):
    """An empty read would look like an empty inbox or state, so it is logged."""
    path = _plant(tmp_path, monkeypatch, planted_by) / "file.json"
    monkeypatch.setattr(module, attr, path)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert not read()
    [record] = [r for r in caplog.records if r.name == module.__name__]
    assert record.levelno == logging.WARNING
    assert str(path) in record.getMessage()
    assert "(PermissionError)" in record.getMessage()


@pytest.mark.parametrize(("module", "attr", "write", "read"), _STORES)
def test_a_dev_file_not_written_yet_reads_as_empty_without_a_warning(
    monkeypatch, tmp_path, caplog, module, attr, write, read
):
    """The first read of a session finds no file; that is not worth a warning."""
    monkeypatch.setattr(module, attr, tmp_path / "socialagent-dev-test" / "file.json")
    with caplog.at_level(logging.DEBUG):
        assert not read()
    assert not caplog.records


@_posix_only
def test_a_symlinked_dir_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "socialagent-dev-test"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError):
        runtime_devfiles.write_private(link / "file.json", "{}")
    assert list(elsewhere.iterdir()) == []


@_posix_only
def test_a_symlinked_file_is_refused_not_followed(tmp_path):
    d = runtime_devfiles.private_dir(tmp_path / "socialagent-dev-test")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (d / "file.json").symlink_to(victim)
    with pytest.raises(OSError):
        runtime_devfiles.write_private(d / "file.json", "{}")
    assert victim.read_text() == "keep me"


def test_dev_state_round_trips_through_the_private_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        state_mod, "_DEV_STATE_PATH", tmp_path / "socialagent-dev-test" / "s.json"
    )
    monkeypatch.setattr(state_mod, "_get_table", lambda: None)
    asyncio.run(state_mod.put("+15555550100", "k", {"v": 1}))
    assert asyncio.run(state_mod.get("+15555550100", "k")) == {"v": 1}


# ── Strands dev sessions: the runtime writes them, the REPL injects and clears ──


def test_dev_sessions_live_in_the_per_user_dir(monkeypatch, tmp_path):
    socialagent_runtime = pytest.importorskip("socialagent_runtime")
    pytest.importorskip("strands.session.file_session_manager")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    manager = socialagent_runtime.create_session_manager(
        {
            "socialagent": {
                "version": 1,
                "memory_id": "__socialagent_dev__",
                "session_id": "session-1",
                "actor_id": "actor-1",
            }
        }
    )
    dev_dir = runtime_devfiles.dev_dir()
    assert dev_dir.parent == tmp_path
    assert manager.storage_dir == str(dev_dir / "sessions")
    assert _mode(dev_dir) == 0o700


def test_the_repl_looks_for_sessions_where_the_runtime_keeps_them(
    monkeypatch, tmp_path
):
    import socialagent.cli.dev as dev

    pytest.importorskip("socialagent_runtime._devfiles")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    dirs = dev._dev_sessions_dirs()
    assert dirs[0] == runtime_devfiles.dev_dir() / "sessions"
    # A vendored copy older than the dev dir still uses Strands' old default.
    assert dirs[1] == tmp_path / "strands" / "sessions"
    monkeypatch.setitem(sys.modules, "socialagent_runtime._devfiles", None)
    assert dev._dev_sessions_dirs() == [tmp_path / "strands" / "sessions"]


def test_reset_clears_the_session_in_both_dirs(monkeypatch, tmp_path):
    import socialagent.cli.dev as dev

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    name = "session_whatsapp-session-conversation-15555550100"
    for d in dev._dev_sessions_dirs():
        (d / name).mkdir(parents=True)
    dev._clear_dev_session("+15555550100")
    assert not any((d / name).exists() for d in dev._dev_sessions_dirs())


def test_injecting_without_a_session_creates_no_directory(monkeypatch, tmp_path):
    """The REPL only adds to a session the runtime wrote, so it never creates
    the dev dir itself, with the default mode, ahead of the runtime."""
    import socialagent.cli.dev as dev

    pytest.importorskip("strands.session.file_session_manager")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    dev._inject_dev_messages_into_session("+15555550100")
    assert list(tmp_path.iterdir()) == []


_PHONE = "+15555550100"


def _plant_session(monkeypatch, sessions: Path) -> Path:
    """A session tree as the runtime leaves it, with one assistant turn to inject.

    Returns the agent's messages directory, where an injected turn would land.
    """
    socialagent_runtime = pytest.importorskip("socialagent_runtime")
    file_session_manager = pytest.importorskip("strands.session.file_session_manager")
    session_id = "whatsapp-session-conversation-15555550100"
    file_session_manager.FileSessionManager(
        session_id=session_id, storage_dir=str(sessions)
    )
    messages = (
        sessions / f"session_{session_id}" / "agents" / "agent_default" / "messages"
    )
    messages.mkdir(parents=True)
    monkeypatch.setitem(
        socialagent_runtime._dev_turns, _PHONE, [("Reminder sent.", "assistant")]
    )
    return messages


def test_injecting_adds_the_turn_to_the_runtime_session(monkeypatch, tmp_path):
    """The control for the refusals below: the planted tree is one the REPL writes to."""
    import socialagent.cli.dev as dev

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    own = runtime_devfiles.private_dir(runtime_devfiles.dev_dir())
    messages = _plant_session(monkeypatch, own / "sessions")
    dev._inject_dev_messages_into_session(_PHONE)
    assert [p.name for p in messages.glob("message_*.json")] == ["message_0.json"]


@_posix_only
@pytest.mark.parametrize("planted_by", ["symlink", "another-user"])
def test_injecting_refuses_a_dev_dir_that_is_not_ours(
    monkeypatch, tmp_path, planted_by
):
    """The REPL checks the dev dir before it writes a turn there, as the runtime
    does, so a session tree another user planted under the predictable name never
    receives a message rendered by an /event handler."""
    import socialagent.cli.dev as dev

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    if planted_by == "symlink":
        planted = tmp_path / "elsewhere"
        planted.mkdir()
        runtime_devfiles.dev_dir().symlink_to(planted, target_is_directory=True)
    else:
        me = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: me + 1)
        planted = runtime_devfiles.dev_dir()
        planted.mkdir(mode=0o777)
    messages = _plant_session(monkeypatch, planted / "sessions")
    dev._inject_dev_messages_into_session(_PHONE)
    assert list(messages.glob("message_*.json")) == []
