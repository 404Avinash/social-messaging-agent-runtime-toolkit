# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Owner-only storage for the files dev mode keeps on disk.

The dev inbox, the dev state, the buffer fallback and the local conversation
sessions live in one per-user directory under the temp dir,
``socialagent-dev-<uid>``. On a shared host every user can write to the temp
dir, and the uid in the name is no secret, so the directory is created 0700
and an existing one is used only if it is a real directory this user owns: a
user who created the name first could otherwise read every file written into
it, or plant one. Files are written 0600 with O_NOFOLLOW, so a symlink in the
directory is refused rather than followed. The directory is checked and the
file is then opened by path, two separate steps, so this relies on nobody else
being able to rename the directory in between; a sticky /tmp and a per-user
TMPDIR (macOS) both rule that out.

The Agent Runtime Package (``packages/socialagent-runtime/_devfiles.py``) and the
Lambda Control Layer (``src/socialagent/utils/devfiles.py``) each keep this same
file, because the two layers never import each other; a test keeps the copies
byte-identical.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def dev_dir() -> Path:
    """Return the per-user directory under the temp dir that holds dev-mode files."""
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"socialagent-dev-{uid}"


def private_dir(path: Path) -> Path:
    """Create ``path`` with mode 0700, or check that an existing one is ours.

    An existing directory this user owns is narrowed to 0700 if it is wider.

    Raises:
        PermissionError: If ``path`` is a symlink, not a directory, or owned by
            another user.
    """
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or (
        hasattr(os, "getuid") and st.st_uid != os.getuid()
    ):
        raise PermissionError(
            f"{path} is not a directory owned by this user, so dev-mode files "
            "are not read from or written to it. Remove it and retry."
        )
    if stat.S_IMODE(st.st_mode) & 0o077:
        path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as an owner-only (0600) file in a 0700 directory."""
    private_dir(path.parent)
    # Create/truncate with mode 0600 so the file is never briefly readable by
    # others, and O_NOFOLLOW so a symlink at ``path`` is refused, not followed.
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as f:
        # The mode passed to open applies only to a new file; one that already
        # exists keeps its own, so narrow it too.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        f.write(text)
