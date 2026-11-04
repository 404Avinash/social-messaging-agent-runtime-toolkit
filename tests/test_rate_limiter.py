# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for the per-user rate limiter.

``tests/test_security_remediations.py`` already covers that the module exists,
that it fails open when disabled, and that Terraform passes the env var. None of
that exercises the limiting itself.

These tests drive the real DynamoDB conditional expressions through moto, since
the limiting logic lives in the expressions. Wall-clock time is monkeypatched rather than
slept through.
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

moto = pytest.importorskip("moto", reason="moto provides the DynamoDB semantics")

_TABLE = "socialagent-test-state"
_PHONE = "+15555550100"


@pytest.fixture
def limiter(monkeypatch):
    """The rate_limiter module wired to a live (moto) table, limit 3/min.

    Yields a ``(module, advance)`` pair: ``advance(seconds)`` moves the clock the
    module reads, so window expiry is exercised without sleeping.
    """
    from moto import mock_aws

    # is_rate_limited builds its client with a bare boto3.resource("dynamodb"),
    # so without a region botocore raises NoRegionError, the broad except swallows
    # it, and the limiter fails open — every assertion below would then be
    # measuring the fail-open path instead of the limiter.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        import socialagent.utils.rate_limiter as rl

        monkeypatch.setattr(rl, "_TABLE_NAME", _TABLE)
        monkeypatch.setattr(rl, "_RATE_LIMIT_PER_MINUTE", 3)

        now = [1_000_000.0]
        monkeypatch.setattr(rl.time, "time", lambda: now[0])

        def advance(seconds: float) -> None:
            now[0] += seconds

        yield rl, advance


def test_paced_conversation_is_never_throttled(limiter):
    """Messages spread wider than the limit must never trip it.

    Two messages a minute against a 3/min limit is not abuse — it is an ordinary
    back-and-forth. The window used to be pushed forward on every accepted
    request, so the counter behaved as a lifetime total and the 4th message of any
    conversation was throttled no matter how slowly it arrived.
    """
    rl, advance = limiter

    for i in range(8):
        assert rl.is_rate_limited(_PHONE) is False, (
            f"message {i + 1}, sent 30s after the last, was throttled — the "
            "window is not resetting"
        )
        advance(30)


def test_burst_over_the_limit_is_throttled(limiter):
    """The limiter must still limit: a same-instant burst is blocked."""
    rl, _ = limiter

    assert [rl.is_rate_limited(_PHONE) for _ in range(5)] == [
        False,
        False,
        False,
        True,
        True,
    ]


def test_window_reopens_after_it_expires(limiter):
    """Once the window passes, a throttled user is allowed again."""
    rl, advance = limiter

    for _ in range(3):
        assert rl.is_rate_limited(_PHONE) is False
    assert rl.is_rate_limited(_PHONE) is True

    advance(61)
    assert rl.is_rate_limited(_PHONE) is False


def test_retrying_while_throttled_does_not_extend_the_block(limiter):
    """A blocked user hammering the bot must not push their own unblock away.

    update_item runs before the limit check, so every rejected attempt also
    counted — and, while the window was being refreshed on write, each rejection
    postponed the reset. The block became indefinite for as long as the user kept
    trying.
    """
    rl, advance = limiter

    # Window opens at t=0 and should close 60s later, whatever happens between.
    for _ in range(3):
        rl.is_rate_limited(_PHONE)
        advance(1)
    assert rl.is_rate_limited(_PHONE) is True  # t=3, over the limit

    for _ in range(3):  # rejected retries at t=13, 23, 33
        advance(10)
        assert rl.is_rate_limited(_PHONE) is True

    advance(32)  # t=65: past 60s from the window's start, not from the last retry
    assert rl.is_rate_limited(_PHONE) is False, (
        "still blocked 65s after the window opened — rejected retries are "
        "pushing the window forward, so the block never clears while the user "
        "keeps trying"
    )


def test_limiter_is_per_user(limiter):
    """One user's burst must not throttle another."""
    rl, _ = limiter
    other = "+15555550199"

    for _ in range(4):
        rl.is_rate_limited(_PHONE)

    assert rl.is_rate_limited(other) is False
