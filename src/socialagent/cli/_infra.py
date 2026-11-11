# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Find the Terraform root a command runs, and manage the copy the CLI ships.

Every command that runs Terraform or reads its variables (``deploy``,
``destroy``, ``bootstrap``, ``status --refresh``, ``config sync`` and
``config tfvars``) resolves the root through :func:`find_tf_dir`, in this
order:

1. ``--tf-dir``, as given.
2. The nearest ``infra-terraform/main.tf`` from the project upward: a SMART
   checkout, which is what contributors and ``make tf-*`` run.
3. The managed copy: the Terraform this CLI ships (``_assets.terraform_dir``),
   copied into ``<project>/.socialagent/infra/<cli version>/``.

The managed copy exists because Terraform writes into the root it runs:
build archives under ``${path.root}/.terraform/`` and
``${path.module}/.terraform/``, the provider lock file, and the bootstrap
root's local state under ``bootstrap/terraform.tfstate.d/``. A shared or
read-only ``site-packages`` cannot take those writes, and two projects must
not share them. The copy carries a marker with the hash of what was copied,
and is replaced whenever its content no longer hashes to the CLI's, so it
is CLI-owned: edits there are overwritten. Terraform's local state is
carried into the replacement. Copies made by other CLI versions are left
where they are, because their bootstrap state, if any, lives in them.

The managed copy cannot deploy yet: ``packaging.tf`` builds the Lambda layer
from the SDK source in the root's parent directory, which only a checkout
has. :func:`check_deployable` says so before Terraform fails on it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from socialagent.cli import _tfvars
from socialagent.cli._assets import terraform_dir
from socialagent.cli._errors import CliError

MARKER = ".socialagent-infra.json"

# What the copy and its hash leave out. Keep in step with the wheel's
# `exclude` in pyproject.toml, so a copy taken from a checkout holds what a
# copy taken from the wheel holds: Terraform's own writes (data dirs, build
# archives, state, the lock file), inputs that name an account (tfvars,
# backend configs, envs/) and the mocked plan tests.
EXCLUDED_DIRS = frozenset(
    {".terraform", ".build", "terraform.tfstate.d", "__pycache__", ".git", ".venv"}
)
EXCLUDED_TOP_DIRS = frozenset({"envs", "tftest"})
EXCLUDED_FILES = (
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.tfvars.json",
    ".terraform.lock.hcl",
    "backend-*.hcl",
    "*.zip",
    "*.pyc",
    ".DS_Store",
    # Credentials, logs and editor files, as the repo .gitignore has them.
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.log",
    "*.swp",
    MARKER,
)
# Terraform's local state, the one thing it writes that cannot be rebuilt.
_STATE_DIRS = frozenset({"terraform.tfstate.d"})
_STATE_FILES = ("terraform.tfstate", "terraform.tfstate.backup")


def managed_root(project_dir: Path) -> Path:
    """Where every version's managed copy lives: ``.socialagent/infra/``."""
    return project_dir / ".socialagent" / "infra"


def is_managed(tf_dir: Path, project_dir: Path) -> bool:
    """Whether *tf_dir* is one of the copies this CLI manages for the project."""
    return tf_dir.resolve().is_relative_to(managed_root(project_dir).resolve())


def tree_files(root: Path) -> list[str]:
    """The files of the Terraform tree under *root*, as sorted POSIX paths."""
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        rel = Path(current).relative_to(root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED_DIRS
            and not (rel == Path(".") and d in EXCLUDED_TOP_DIRS)
        )
        for name in files:
            if any(fnmatch.fnmatchcase(name, p) for p in EXCLUDED_FILES):
                continue
            found.append((rel / name).as_posix())
    return sorted(found)


def tree_hash(root: Path) -> str:
    """SHA-256 over the relative path and bytes of every file in the tree."""
    digest = hashlib.sha256()
    for rel in tree_files(root):
        digest.update(rel.encode() + b"\0")
        digest.update((root / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def _cli_version() -> str:
    from socialagent.cli import _version  # noqa: PLC0415 — cli/__init__ imports the command modules

    # A version is a directory name here, so nothing but a plain token is used.
    return re.sub(r"[^A-Za-z0-9._+-]", "_", _version()) or "unknown"


def _marker_hash(dest: Path) -> str | None:
    try:
        return json.loads((dest / MARKER).read_text())["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _state_paths(root: Path) -> list[Path]:
    """Terraform's local state under *root*, relative to it."""
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        rel = Path(current).relative_to(root)
        for d in [d for d in dirs if d in _STATE_DIRS]:
            found.append(rel / d)
            dirs.remove(d)
        dirs[:] = [d for d in dirs if d != ".terraform"]
        found += [rel / f for f in files if f in _STATE_FILES]
    return found


def materialize(
    project_dir: Path, source: Path | None = None, version: str | None = None
) -> Path:
    """Return the project's managed copy of *source*, (re)creating it if needed.

    *source* defaults to the Terraform this CLI ships and *version* to the
    CLI's version. A copy whose marker and content both hash to *source* is
    used as it is; any other (missing, partial, edited, or from a different
    build of the same version) is rebuilt beside it and swapped in, with
    Terraform's local state carried over.
    """
    source = (source or terraform_dir()).resolve()
    if not (source / "main.tf").is_file():
        raise CliError(
            "could not find infra-terraform/ — run from inside the SMART "
            "checkout, or pass --tf-dir"
        )
    version = version or _cli_version()
    root = managed_root(project_dir)
    dest = root / version
    digest = tree_hash(source)
    if _marker_hash(dest) == digest and tree_hash(dest) == digest:
        return dest

    _tfvars.work_dir(project_dir)
    root.mkdir(exist_ok=True)
    staging = root / f".{version}.new-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    for rel in tree_files(source):
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    replacing = dest.is_dir()
    if replacing:
        for rel in _state_paths(dest):
            (staging / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dest / rel, staging / rel)
    (staging / MARKER).write_text(
        json.dumps({"version": version, "sha256": digest}, indent=2) + "\n"
    )
    if replacing:
        old = root / f".{version}.old-{os.getpid()}"
        dest.rename(old)
        staging.rename(dest)
        shutil.rmtree(old, ignore_errors=True)
    else:
        staging.rename(dest)
    shown = os.path.relpath(dest, project_dir)
    # stderr, so `config` output piped to another program stays parseable.
    print(
        f"Terraform: this CLI's copy, {'refreshed' if replacing else 'written'} to "
        f"{shown} (managed by socialagent: edits there are overwritten)",
        file=sys.stderr,
    )
    return dest


def _checkout(start: Path) -> Path | None:
    start = start.resolve()
    return next(
        (
            p / "infra-terraform"
            for p in (start, *start.parents)
            if (p / "infra-terraform" / "main.tf").is_file()
        ),
        None,
    )


def find_tf_dir(
    project_dir: Path, override: str = "", *, materialize_copy: bool = True
) -> Path:
    """The Terraform root for *project_dir*: ``--tf-dir``, a checkout, else the managed copy.

    With ``materialize_copy=False`` nothing is written: the last step returns
    the packaged source instead of the project's copy, for callers that only
    need to know a root exists.
    """
    if override:
        tf = Path(override).resolve()
        if not (tf / "main.tf").is_file():
            raise CliError(f"--tf-dir {override}: no main.tf there")
        return tf
    checkout = _checkout(project_dir)
    if checkout is not None:
        return checkout
    if not materialize_copy:
        source = terraform_dir()
        if not (source / "main.tf").is_file():
            raise CliError(
                "could not find infra-terraform/ — run from inside the SMART "
                "checkout, or pass --tf-dir"
            )
        return source
    return materialize(project_dir)


def check_deployable(tf_dir: Path, project_dir: Path, command: str) -> None:
    """Refuse ``deploy``/``destroy`` from the managed copy, which cannot build the layer.

    ``packaging.tf`` zips the SDK from ``${path.root}/..`` and reads
    ``pyproject.toml`` there with ``file()``, so every plan of the managed copy
    (a destroy plans too) fails on a path that only a checkout has.
    """
    if (
        not is_managed(tf_dir, project_dir)
        or (tf_dir.parent / "pyproject.toml").is_file()
    ):
        return
    raise CliError(
        f"`socialagent {command}` cannot run from the Terraform this CLI manages "
        f"({tf_dir}) yet: packaging.tf builds the Lambda layer from the SDK source "
        "next to infra-terraform/ (pyproject.toml, src/socialagent), which only a "
        "SMART checkout has. Run it from a project inside the checkout, or pass "
        "--tf-dir <checkout>/infra-terraform. bootstrap, status --refresh and "
        "config work from the managed copy."
    )
