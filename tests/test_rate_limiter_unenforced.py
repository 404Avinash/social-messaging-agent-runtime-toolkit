# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A rate limit with no state table to enforce it is logged, not silent.

The limiter keeps its counters in the state table, so without one it allows
every message. The Terraform defaults are a limit of 20 and the state table off,
which leaves a stack that looks limited and is not. The limiter still fails open
(changing the default deployment is out of scope), but says so once at cold
start.
"""

from __future__ import annotations

import importlib
import logging

import pytest

import socialagent.utils.rate_limiter as rl

_LOGGER = "socialagent.utils.rate_limiter"


def _unenforced(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "NOT enforced" in r.getMessage()]


@pytest.mark.parametrize(
    ("limit", "table", "warns"),
    [
        (20, "", True),
        (1, "", True),
        (0, "", False),  # rate limiting off: nothing to enforce
        (-1, "", False),
        (20, "dev-socialagent-state", False),  # enforced
    ],
)
def test_warns_only_when_a_limit_has_no_table(limit, table, warns, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert rl._warn_if_unenforced(limit, table) is warns
    records = _unenforced(caplog)
    assert len(records) == (1 if warns else 0)
    if warns:
        assert records[0].levelno == logging.WARNING
        assert str(limit) in records[0].getMessage()


def test_cold_start_warns_once_and_still_fails_open(caplog):
    """The check runs when the module is imported, from the real environment.

    Reloading stands in for a cold start. Messages after it are still allowed
    (the default deployment is unchanged) and do not repeat the warning.
    """
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("SOCIALAGENT_RATE_LIMIT_PER_MINUTE", "20")
            mp.delenv("STATE_TABLE_NAME", raising=False)
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                importlib.reload(rl)
                assert len(_unenforced(caplog)) == 1

                for _ in range(25):
                    assert rl.is_rate_limited("+15555550100") is False
                assert len(_unenforced(caplog)) == 1
    finally:
        importlib.reload(rl)  # back to the test environment's configuration


def test_cold_start_is_quiet_with_a_table(caplog):
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("SOCIALAGENT_RATE_LIMIT_PER_MINUTE", "20")
            mp.setenv("STATE_TABLE_NAME", "dev-socialagent-state")
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                importlib.reload(rl)
        assert _unenforced(caplog) == []
    finally:
        importlib.reload(rl)
