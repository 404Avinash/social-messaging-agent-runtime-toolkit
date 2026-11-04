# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Find the project a command is being run in.

Every project-scoped command (``recipe add``, ``config``, ``templates sync``,
``dev``) resolves its files from the same place: the nearest directory, from
the current one upward, that holds a ``socialagent.<env>.yaml`` or an
``agent.py`` — the two files every scaffolded project has. So a command run
from ``src/tools/`` acts on the project, and every command resolves it the
same way.

The walk stops at a git repository root, so a stray file above the project —
say an ``agent.py`` in the home directory — can never capture it. When no
project is found the current directory is used, which keeps ``recipe add``
working in a fresh directory.
"""

from __future__ import annotations

from pathlib import Path


def _is_project(directory: Path) -> bool:
    if (directory / "agent.py").is_file():
        return True
    return any(p.is_file() for p in directory.glob("socialagent.*.yaml"))


def find_project_root(start: Path | None = None) -> Path | None:
    """Return the nearest enclosing project directory, or ``None``."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if _is_project(directory):
            return directory
        if (directory / ".git").exists():
            return None
    return None


def project_root(start: Path | None = None) -> Path:
    """The enclosing project directory, falling back to *start* (default cwd)."""
    base = (start or Path.cwd()).resolve()
    return find_project_root(base) or base
