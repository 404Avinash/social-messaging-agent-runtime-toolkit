# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A project's vendored socialagent_runtime/ says whether it matches the CLI.

Fixes to packages/socialagent-runtime/ reach a project only when its copy is
replaced, and nothing used to flag an old copy. ``init`` now stamps the copy,
``socialagent runtime status`` and the ``dev``/``deploy`` warnings compare it
against every runtime this CLI's git history has shipped, and ``socialagent
runtime update`` replaces an older copy with a backup and refuses an unknown one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from socialagent.cli import runtime_cmd
from socialagent.cli._errors import CliError
from socialagent.cli.init import cmd_init


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal-blueprint project scaffolded outside the repo, with cwd inside it."""
    monkeypatch.chdir(tmp_path)
    cmd_init(argparse.Namespace(name="my-agent", blueprint=None, list_blueprints=False))
    root = tmp_path / "my-agent"
    monkeypatch.chdir(root)
    return root


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


@pytest.fixture
def lineage(tmp_path, monkeypatch):
    """A fake SMART checkout whose runtime has two commits: v1, then v2 (a fix).

    The CLI is pointed at this checkout's runtime, so its lineage is {v1, v2}
    and its current runtime is v2. Returns the paths to each snapshot. Skipped
    where git is not installed (the CI test image); the product degrades the
    same way, which ``test_lineage_without_git_is_just_the_current_runtime``
    covers everywhere.
    """
    if shutil.which("git") is None:
        if os.environ.get("CI"):
            pytest.fail("git is not on PATH in CI; the lineage tests must run there")
        pytest.skip("git is not on PATH; the lineage comes from git history")
    repo = tmp_path / "smart-checkout"
    runtime = repo / "packages" / "socialagent-runtime"
    shutil.copytree(
        runtime_cmd.socialagent_runtime_dir(),
        runtime,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    v1 = tmp_path / "v1"
    shutil.copytree(runtime, v1, ignore=shutil.ignore_patterns("__pycache__"))

    (runtime / "scheduler.py").write_text(
        (runtime / "scheduler.py").read_text() + "\n# a later fix\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    monkeypatch.setattr(runtime_cmd, "socialagent_runtime_dir", lambda: runtime)
    return {"repo": repo, "runtime": runtime, "v1": v1}


def _stamp(project: Path) -> dict:
    return json.loads(
        (project / "socialagent_runtime" / runtime_cmd.STAMP_NAME).read_text()
    )


def _install(project: Path, snapshot: Path) -> None:
    """Make the project's vendored copy be ``snapshot``, stamped as init would."""
    target = project / "socialagent_runtime"
    shutil.rmtree(target)
    shutil.copytree(snapshot, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / runtime_cmd.STAMP_NAME).write_text(
        json.dumps({"content_sha256": runtime_cmd.content_hash(target)})
    )


def test_init_stamps_the_copy(project):
    stamp = _stamp(project)
    assert stamp == {
        "content_sha256": runtime_cmd.content_hash(project / "socialagent_runtime")
    }
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT


def test_bytecode_does_not_change_the_hash(project):
    cache = project / "socialagent_runtime" / "__pycache__"
    cache.mkdir()
    (cache / "notify.cpython-312.pyc").write_bytes(b"\x00")
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT


def test_os_and_editor_files_do_not_count_as_edits(project):
    runtime = project / "socialagent_runtime"
    (runtime / ".DS_Store").write_bytes(b"\x00\x01")
    (runtime / "notify.py.swp").write_bytes(b"swap")
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT


def test_crlf_checkout_is_not_an_edit(project):
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_bytes(notify.read_bytes().replace(b"\n", b"\r\n"))
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT


def test_init_copies_only_the_runtime_modules(project):
    runtime = project / "socialagent_runtime"
    shipped = {p.name for p in runtime.iterdir()}
    assert shipped - {runtime_cmd.STAMP_NAME} == {
        p.name for p in runtime_cmd.socialagent_runtime_dir().glob("*.py")
    }


def test_copy_without_stamp_is_judged_by_its_hash(project, capsys):
    """A pre-stamp project is classified like any other; no warning when it matches."""
    (project / "socialagent_runtime" / runtime_cmd.STAMP_NAME).unlink()
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT
    runtime_cmd.warn_if_stale(project)
    assert capsys.readouterr().err == ""


def test_unstamped_edited_copy_is_unknown_and_refused(project):
    """The protection applies to every project that exists today, stamp or not."""
    runtime = project / "socialagent_runtime"
    (runtime / runtime_cmd.STAMP_NAME).unlink()
    notify = runtime / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    assert runtime_cmd.check(project).status == runtime_cmd.UNKNOWN
    with pytest.raises(CliError, match="--force"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))
    assert "# local edit" in notify.read_text()


def test_unstamped_copy_from_a_later_cli_is_refused(project, lineage):
    """A copy hand-copied from a newer checkout, as the old docs said to do."""
    _install(project, lineage["runtime"])
    (project / "socialagent_runtime" / runtime_cmd.STAMP_NAME).unlink()
    _git(lineage["repo"], "reset", "-q", "--hard", "HEAD~1")
    assert runtime_cmd.check(project).status == runtime_cmd.UNKNOWN
    with pytest.raises(CliError, match="--force"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))


def test_unstamped_older_copy_is_outdated_and_updates(project, lineage):
    _install(project, lineage["v1"])
    (project / "socialagent_runtime" / runtime_cmd.STAMP_NAME).unlink()
    assert runtime_cmd.check(project).status == runtime_cmd.OUTDATED
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))
    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT


def test_current_copy_prints_no_warning(project, capsys):
    runtime_cmd.warn_if_stale(project)
    assert capsys.readouterr().err == ""


def test_lineage_holds_every_runtime_the_checkout_has_shipped(lineage):
    hashes = runtime_cmd.lineage_hashes(lineage["runtime"])
    assert runtime_cmd.content_hash(lineage["v1"]) in hashes
    assert runtime_cmd.content_hash(lineage["runtime"]) in hashes
    assert len(hashes) == 2


def test_lineage_hashes_non_ascii_sources_byte_exact(lineage, monkeypatch):
    """A git blob with non-ASCII must hash like the file on disk under any locale."""
    runtime = lineage["runtime"]
    assert any(b"\xe2\x8f\xb0" in p.read_bytes() for p in runtime.glob("*.py")), (
        "fixture lost the clock emoji the real scheduler.py carries"
    )
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    hashes = runtime_cmd.lineage_hashes(runtime)
    assert runtime_cmd.content_hash(lineage["v1"]) in hashes
    assert runtime_cmd.content_hash(runtime) in hashes


def test_lineage_without_git_is_just_the_current_runtime(tmp_path, monkeypatch):
    """A wheel install has no history; it recognizes only its own runtime."""
    src = tmp_path / "wheel" / "cli" / "_assets" / "socialagent-runtime"
    shutil.copytree(
        runtime_cmd.socialagent_runtime_dir(),
        src,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setattr(runtime_cmd, "_git", lambda *a, **k: None)
    assert runtime_cmd.lineage_hashes(src) == {runtime_cmd.content_hash(src)}


def test_unknown_without_history_says_the_cli_cannot_tell(project, monkeypatch):
    """A wheel install must not accuse the user of editing; it cannot know."""
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# whatever\n")
    monkeypatch.setattr(runtime_cmd, "_git", lambda *a, **k: None)
    result = runtime_cmd.check(project)
    assert result.status == runtime_cmd.UNKNOWN
    assert "no git history" in runtime_cmd.describe(result)


def test_warn_if_stale_never_raises(project, monkeypatch, capsys):
    monkeypatch.setattr(runtime_cmd, "check", lambda _p: 1 / 0)
    runtime_cmd.warn_if_stale(project)
    assert "could not check" in capsys.readouterr().err


def test_force_does_not_tell_the_user_to_pass_force(project, capsys):
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# edit\n")
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=True))
    assert "pass --force" not in capsys.readouterr().out


def test_eof_at_the_prompt_cancels(project, lineage, monkeypatch, capsys):
    _install(project, lineage["v1"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(EOFError()))
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=False, force=False))
    assert "Cancelled." in capsys.readouterr().out
    assert runtime_cmd.check(project).status == runtime_cmd.OUTDATED


def test_symlinked_runtime_is_refused_before_any_backup(project, lineage, tmp_path):
    _install(project, lineage["v1"])
    real = tmp_path / "elsewhere"
    shutil.move(project / "socialagent_runtime", real)
    (project / "socialagent_runtime").symlink_to(real, target_is_directory=True)
    with pytest.raises(CliError, match="symlink"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=True))
    assert not list((project / ".socialagent" / "runtime-backups").glob("*"))
    assert real.is_dir()


def test_update_gitignores_the_backup_dir_before_the_first_deploy(project, lineage):
    _install(project, lineage["v1"])
    assert not (project / ".socialagent" / ".gitignore").exists()
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))
    assert (project / ".socialagent" / ".gitignore").read_text().strip() == "*"


def test_older_copy_in_the_lineage_is_outdated(project, lineage, capsys):
    _install(project, lineage["v1"])
    result = runtime_cmd.check(project)
    assert result.status == runtime_cmd.OUTDATED
    runtime_cmd.warn_if_stale(project)
    assert "socialagent runtime update" in capsys.readouterr().err


def test_update_replaces_an_outdated_copy_and_keeps_a_backup(project, lineage):
    _install(project, lineage["v1"])
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))

    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT
    assert (
        "# a later fix"
        in (project / "socialagent_runtime" / "scheduler.py").read_text()
    )
    backups = list((project / ".socialagent" / "runtime-backups").glob("*"))
    assert len(backups) == 1
    assert "# a later fix" not in (backups[0] / "scheduler.py").read_text()


def test_status_exits_nonzero_when_not_current(project, lineage):
    _install(project, lineage["v1"])
    with pytest.raises(SystemExit) as exc:
        runtime_cmd.cmd_runtime_status(argparse.Namespace())
    assert exc.value.code == 1


def test_update_without_yes_needs_a_terminal(project, lineage, monkeypatch):
    _install(project, lineage["v1"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    before = runtime_cmd.content_hash(project / "socialagent_runtime")
    with pytest.raises(CliError, match="--yes"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=False, force=False))
    assert runtime_cmd.content_hash(project / "socialagent_runtime") == before
    assert not list((project / ".socialagent" / "runtime-backups").glob("*"))


def test_copy_from_a_later_cli_is_unknown_and_never_downgraded(project, lineage):
    """An older CLI (at v1) must not replace a copy written by a later one (v2)."""
    v2 = lineage["runtime"]
    _install(project, v2)
    # Rewind the CLI to v1: v2 is no longer in its lineage.
    _git(lineage["repo"], "reset", "-q", "--hard", "HEAD~1")

    result = runtime_cmd.check(project)
    assert result.status == runtime_cmd.UNKNOWN
    assert "never shipped" in runtime_cmd.describe(result)
    before = runtime_cmd.content_hash(project / "socialagent_runtime")
    with pytest.raises(CliError, match="--force"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))
    assert runtime_cmd.content_hash(project / "socialagent_runtime") == before


def test_edited_copy_is_unknown_and_update_refuses_it(project):
    """A local edit is content no CLI shipped; update must not discard it silently."""
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    result = runtime_cmd.check(project)
    assert result.status == runtime_cmd.UNKNOWN
    with pytest.raises(CliError, match="--force"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))
    assert "# local edit" in notify.read_text()


def test_edited_copy_from_a_later_cli_is_still_refused(project, lineage):
    """Edited AND newer: the edit must not reclassify it into something update replaces."""
    _install(project, lineage["runtime"])
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    _git(lineage["repo"], "reset", "-q", "--hard", "HEAD~1")

    assert runtime_cmd.check(project).status == runtime_cmd.UNKNOWN
    with pytest.raises(CliError, match="--force"):
        runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=False))


def test_force_replaces_an_unknown_copy_with_a_backup(project):
    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    runtime_cmd.cmd_runtime_update(argparse.Namespace(yes=True, force=True))

    assert runtime_cmd.check(project).status == runtime_cmd.CURRENT
    backups = list((project / ".socialagent" / "runtime-backups").glob("*"))
    assert len(backups) == 1
    assert "# local edit" in (backups[0] / "notify.py").read_text()
