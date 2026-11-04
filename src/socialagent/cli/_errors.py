# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One failure convention for every ``socialagent`` command.

A command that hits a *user* mistake — bad input, a missing file, an invalid
config — raises :class:`CliError` with a message a developer can act on.
``socialagent.cli.main`` catches it, prints ``ERROR: <message>`` to stderr and
exits 1. Anything else is a bug and still produces a traceback, because that is
where a traceback is useful.
"""

from __future__ import annotations

# The environments Terraform accepts (`environment` validation in
# infra-terraform/variables.tf) and config_file._validate enforces. Every
# `--env` flag takes its choices from here, so an unusable name is rejected
# before anything is written.
ENVIRONMENTS = ("dev", "test", "prod")


class CliError(Exception):
    """A user-facing failure: the message is printed as ``ERROR: <message>``."""


class ConfigError(CliError, ValueError):
    """An unreadable or invalid ``socialagent.<env>.yaml``.

    Also a ``ValueError``: ``config_file.load`` documents that it raises
    ``ValueError``, and callers may catch that.
    """
