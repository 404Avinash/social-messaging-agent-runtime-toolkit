# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep a project's vendored ``socialagent_runtime/`` in step with the CLI.

``socialagent init`` copies ``packages/socialagent-runtime/`` into each project,
and ``socialagent deploy`` ships that copy as it is. A fix to the runtime (the
scheduler ownership check, the restored-session filter) therefore reaches an
existing project only when its copy is replaced, and ``SOCIALAGENT_VERSION``
only moves when the runtime-to-Lambda payload contract does.

So the CLI hashes the copy's modules and knows the hash of every runtime its
own history has shipped (one per commit that touched
``packages/socialagent-runtime/``). That lineage answers the only question that
matters: does the project's copy contain anything this CLI has never shipped?
The stamp ``init`` writes records which hash was vendored, for a human reading
the directory; the classification never depends on it, so a copy that predates
stamping, or lost its stamp, is judged exactly like any other.

- the copy hashes to the CLI's runtime: **current**;
- the copy hashes to an earlier runtime in this CLI's lineage: **outdated**,
  and ``runtime update`` replaces it;
- the copy hashes to nothing this CLI has shipped: **unknown**. It may come
  from a later CLI, a fork, or a local edit; ``runtime update`` refuses it
  unless ``--force``, so it never downgrades or discards edits.

Nothing in the stamp is hand-maintained: no version to compare, no counter to
bump, so two checkouts can never disagree about which copy is newer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from socialagent.cli._assets import socialagent_runtime_dir
from socialagent.cli._errors import CliError
from socialagent.cli._project import project_root

RUNTIME_DIRNAME = "socialagent_runtime"
STAMP_NAME = ".socialagent-runtime.json"
# Under .socialagent/, which the agent source archives exclude, so a backup is
# never uploaded or built with the agent.
BACKUP_DIR = Path(".socialagent") / "runtime-backups"
# Where packages/socialagent-runtime/ lives relative to the repo root; the git
# path the lineage is read from.
_RUNTIME_REPO_PATH = "packages/socialagent-runtime"

# Status values, from least to most attention needed.
CURRENT = "current"
OUTDATED = "outdated"
UNKNOWN = "unknown"
MISSING = "missing"


def _modules(directory: Path) -> list[Path]:
    """The runtime's Python modules: what ships and what the hash covers.

    Only ``*.py`` counts, so OS and editor files (``.DS_Store``, swap files,
    tool caches) never make an untouched copy look edited.
    """
    return sorted(
        p
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.relative_to(directory).parts
    )


def _hash_modules(modules: list[tuple[str, bytes]]) -> str:
    """SHA-256 over (relative posix path, source) pairs, sorted by path.

    Line endings are normalized, so a Windows checkout with core.autocrlf does
    not differ from the copy the CLI ships. Both a directory on disk and a set
    of git blobs hash through here, so the two can never drift.
    """
    digest = hashlib.sha256()
    for rel, source in sorted(modules):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(source.replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def content_hash(directory: Path) -> str:
    """Hash of the runtime modules under ``directory``."""
    return _hash_modules(
        [
            (p.relative_to(directory).as_posix(), p.read_bytes())
            for p in _modules(directory)
        ]
    )


def _git(repo: Path, *args: str, stdin: bytes = b"") -> bytes | None:
    """Run git in ``repo`` and return raw stdout, or None if git cannot answer.

    Bytes, never text: the runtime's sources contain non-ASCII (a clock emoji
    in ``scheduler.py``), and decoding them with the user's locale either
    raises on Windows (cp1252) or silently changes the hash (ISO-8859-1).
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            input=stdin,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _parse_cat_file_batch(out: bytes) -> list[bytes | None]:
    """Parse ``git cat-file --batch`` output into one entry per requested object, in order.

    Each record is ``<sha> blob <size>\\n<content>\\n``; a missing object is
    ``<spec> missing\\n`` and yields None. The header carries the resolved object
    SHA, not the ``commit:path`` that was requested, so records are matched to
    requests by position, which ``cat-file`` guarantees.
    """
    records: list[bytes | None] = []
    i = 0
    while i < len(out):
        nl = out.index(b"\n", i)
        header = out[i:nl].split(b" ")
        i = nl + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            records.append(out[i : i + size])
            i += size + 1
        else:
            records.append(None)
    return records


def has_history(runtime_dir: Path | None = None) -> bool:
    """True when the CLI runs from a git checkout that holds the runtime's history."""
    runtime_dir = runtime_dir or socialagent_runtime_dir()
    revs = _git(runtime_dir.parent.parent, "rev-list", "HEAD", "--", _RUNTIME_REPO_PATH)
    return bool(revs and revs.strip())


def lineage_hashes(runtime_dir: Path | None = None) -> frozenset[str]:
    """Hashes of every runtime this CLI's history has shipped, oldest included.

    One per commit that touched the runtime directory, each hashed exactly as
    a vendored copy is. The current working tree's hash is always a member, so
    a CLI run from a checkout with uncommitted runtime edits still recognizes
    its own copy. Just the current hash when the CLI does not run from a git
    checkout (a wheel install, a shallow clone, a ZIP); every non-current copy
    is then ``unknown``, never guessed at.

    ``rev-list`` rather than ``log`` so the user's ``log.*`` config cannot change
    the answer (``log.follow`` drops merge states; ``log.showSignature`` runs
    ``gpg.program``). All blobs come back from a single ``cat-file --batch``,
    so a lineage of N commits costs 2 + N git processes, not 2 + N * modules.
    """
    runtime_dir = runtime_dir or socialagent_runtime_dir()
    hashes = {content_hash(runtime_dir)}
    repo = runtime_dir.parent.parent
    revs = _git(repo, "rev-list", "HEAD", "--", _RUNTIME_REPO_PATH)
    if not revs:
        return frozenset(hashes)
    commits = revs.decode().split()
    # Which .py paths exist in each commit, so the cat-file request is exact.
    wanted: list[tuple[str, str]] = []  # (commit, path)
    for commit in commits:
        listing = _git(repo, "ls-tree", "-r", "--name-only", commit, _RUNTIME_REPO_PATH)
        if not listing:
            continue
        for rel in listing.decode("utf-8", "surrogateescape").split("\n"):
            if rel.endswith(".py") and "__pycache__" not in rel:
                wanted.append((commit, rel))
    if not wanted:
        return frozenset(hashes)
    specs = "".join(f"{c}:{r}\n" for c, r in wanted).encode()
    out = _git(repo, "cat-file", "--batch", stdin=specs)
    if out is None:
        return frozenset(hashes)
    blobs = _parse_cat_file_batch(out)
    if len(blobs) != len(wanted):
        return frozenset(hashes)
    by_commit: dict[str, list[tuple[str, bytes]]] = {}
    for (commit, rel), blob in zip(wanted, blobs, strict=True):
        if blob is None:
            continue
        posix = Path(rel).relative_to(_RUNTIME_REPO_PATH).as_posix()
        by_commit.setdefault(commit, []).append((posix, blob))
    for modules in by_commit.values():
        hashes.add(_hash_modules(modules))
    return frozenset(hashes)


def vendor_runtime(project_dir: Path) -> Path:
    """Copy the CLI's runtime into ``project_dir/socialagent_runtime/`` and stamp it."""
    src = socialagent_runtime_dir()
    if not src.exists():
        raise CliError(f"socialagent-runtime source not found at {src}")
    target = project_dir / RUNTIME_DIRNAME
    if target.exists():
        shutil.rmtree(target)
    for module in _modules(src):
        dest = target / module.relative_to(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module, dest)
    # Informational: says which runtime was vendored. check() never reads it.
    stamp = {"content_sha256": content_hash(target)}
    (target / STAMP_NAME).write_text(
        json.dumps(stamp, indent=2) + "\n", encoding="utf-8"
    )
    return target


@dataclass(frozen=True)
class RuntimeStatus:
    status: str


def check(project_dir: Path) -> RuntimeStatus:
    """Compare the project's vendored runtime with the runtimes this CLI has shipped.

    The copy is classified by the hash of its modules alone. The stamp file is
    informational: a copy that predates stamping, or whose stamp was deleted,
    gets the same answer as one with a stamp, so the protection against
    replacing a newer or edited copy applies to every project that exists.
    """
    target = project_dir / RUNTIME_DIRNAME
    if not target.is_dir():
        return RuntimeStatus(MISSING)
    actual = content_hash(target)
    if actual == content_hash(socialagent_runtime_dir()):
        return RuntimeStatus(CURRENT)
    if actual in lineage_hashes():
        return RuntimeStatus(OUTDATED)
    return RuntimeStatus(UNKNOWN)


def describe(result: RuntimeStatus) -> str:
    """One line a person can act on."""
    if result.status == CURRENT:
        return "socialagent_runtime/ matches the runtime this CLI ships."
    if result.status == OUTDATED:
        return (
            "socialagent_runtime/ is an earlier runtime than this CLI ships. Run "
            "`socialagent runtime update` to take this CLI's runtime fixes."
        )
    if result.status == UNKNOWN:
        if not has_history():
            return (
                "socialagent_runtime/ differs from the runtime this CLI ships, and this "
                "CLI has no git history to tell an earlier copy from a later or edited "
                "one. `socialagent runtime update --force` replaces it (it keeps a backup); "
                "a CLI run from a full checkout can tell which it is."
            )
        return (
            "socialagent_runtime/ has content this CLI has never shipped: a later "
            "CLI wrote it, or it was edited after copying. `socialagent runtime update` "
            "refuses to replace it; pass --force to replace it anyway (it keeps a backup)."
        )
    return "No socialagent_runtime/ in this project."


def warn_if_stale(project_dir: Path) -> None:
    """Print a warning to stderr unless the vendored runtime matches this CLI.

    Never blocks and never raises: a project may pin an older runtime on
    purpose, and a failure to read git or the files must not stop ``dev`` or
    ``deploy``, which only call this for the warning.
    """
    try:
        result = check(project_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not check socialagent_runtime/: {exc}", file=sys.stderr)
        return
    if result.status in (CURRENT, MISSING):
        return
    print(f"WARNING: {describe(result)}", file=sys.stderr)


def cmd_runtime_status(_args: argparse.Namespace) -> None:
    """``socialagent runtime status``. Exits 1 unless the copy matches this CLI."""
    result = check(project_root())
    print(describe(result))
    if result.status != CURRENT:
        sys.exit(1)


def cmd_runtime_update(args: argparse.Namespace) -> None:
    """``socialagent runtime update [--yes] [--force]``: replace the copy, keeping a backup.

    Replaces an ``outdated`` copy. An ``unknown`` copy, which may be newer than
    this CLI or carry local edits, needs ``--force``.
    """
    project_dir = project_root()
    result = check(project_dir)
    force = getattr(args, "force", False)
    if result.status == CURRENT:
        print(describe(result))
        return
    if result.status == MISSING:
        raise CliError(
            f"no {RUNTIME_DIRNAME}/ in {project_dir}; is this a SMART project?"
        )
    if result.status == UNKNOWN and not force:
        raise CliError(describe(result))
    print(
        describe(result) if not force else "Replacing socialagent_runtime/ (--force)."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError(
                "runtime update needs confirmation and stdin is not a terminal — pass --yes"
            )
        try:
            answer = input("Replace socialagent_runtime/ with this CLI's copy? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            print("Cancelled.")
            return
    current = project_dir / RUNTIME_DIRNAME
    if current.is_symlink():
        raise CliError(
            f"{RUNTIME_DIRNAME}/ is a symlink; replace the link target yourself, "
            "runtime update does not follow it"
        )
    backup = project_dir / BACKUP_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # The backup keeps everything in the old copy, edits and extra files included.
    shutil.copytree(
        current, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    _ensure_backups_ignored(project_dir)
    vendor_runtime(project_dir)
    print("✓ socialagent_runtime/ updated to the runtime this CLI ships")
    print(f"  Previous copy: {backup.relative_to(project_dir)}/")
    print("  Redeploy for the agent to run it: socialagent deploy --env <env>")


def _ensure_backups_ignored(project_dir: Path) -> None:
    """Make sure ``.socialagent/`` is gitignored before the first backup lands in it.

    ``_tfvars.write`` creates ``.socialagent/.gitignore`` on the first deploy; a
    project that runs ``runtime update`` before ever deploying would otherwise
    commit its backups with ``git add -A``.
    """
    ignore = project_dir / ".socialagent" / ".gitignore"
    if not ignore.exists():
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text("*\n", encoding="utf-8")
