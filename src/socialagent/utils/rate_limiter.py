# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-user rate limiting using DynamoDB atomic counters with TTL.

Uses the same DynamoDB state table (if configured) to track message counts
per phone number within a sliding time window. If no state table is configured
or rate limiting is disabled, all messages are allowed through; a limit set
with no table to enforce it is logged as a warning once, at cold start.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_RATE_LIMIT_PER_MINUTE = int(os.environ.get("SOCIALAGENT_RATE_LIMIT_PER_MINUTE", "0"))
_WINDOW_SECONDS = 60
_TABLE_NAME = os.environ.get("STATE_TABLE_NAME", "")


def _warn_if_unenforced(limit: int, table_name: str) -> bool:
    """Log a WARNING when a limit is configured but cannot be enforced.

    The counters live in the state table, so with no table every message is
    allowed and the configured limit does nothing. The Terraform defaults (a
    limit of 20, the state table off) produce exactly that, and nothing else
    would surface it. Returns True when it warned.
    """
    if limit <= 0 or table_name:
        return False
    logger.warning(
        "SOCIALAGENT_RATE_LIMIT_PER_MINUTE=%d but STATE_TABLE_NAME is empty: "
        "per-user rate limiting is NOT enforced and every message is allowed. "
        "Enable the state table (enable_state_table = true) to enforce it.",
        limit,
    )
    return True


# Once per execution environment: the module is imported at cold start.
_warn_if_unenforced(_RATE_LIMIT_PER_MINUTE, _TABLE_NAME)


def _rate_limit_key(phone: str) -> str:
    h = hashlib.sha256(phone.encode()).hexdigest()[:16]
    return f"_ratelimit#{h}"


def _count_against_open_window(table, pk: str, now: int) -> bool:
    """Increment against a window another invocation opened; True if over limit.

    Reached when two invocations race to retire an expired window: one wins the
    conditional put, the loser must count itself into the winner's window rather
    than reset it. Fails open, like every other error path here.
    """
    try:
        resp = table.update_item(
            Key={"pk": pk, "sk": "count"},
            UpdateExpression="SET #cnt = if_not_exists(#cnt, :zero) + :one",
            ExpressionAttributeNames={"#cnt": "msg_count"},
            ExpressionAttributeValues={":zero": 0, ":one": 1},
            ReturnValues="ALL_NEW",
        )
    except Exception:
        logger.debug("Rate limiter re-count failed, allowing message")
        return False
    count = resp["Attributes"].get("msg_count", 0)
    if count > _RATE_LIMIT_PER_MINUTE:
        logger.warning(
            "Rate limit exceeded for user %s (%d/%d in window)",
            pk,
            count,
            _RATE_LIMIT_PER_MINUTE,
        )
        return True
    return False


def is_rate_limited(phone: str) -> bool:
    """Return True if the user has exceeded the per-minute message limit.

    Returns False (allow) when:
    - SOCIALAGENT_RATE_LIMIT_PER_MINUTE is 0 or unset (feature disabled)
    - STATE_TABLE_NAME is empty (no DynamoDB table available; warned at import)
    - DynamoDB call fails (fail-open to avoid blocking legitimate users)
    """
    if _RATE_LIMIT_PER_MINUTE <= 0 or not _TABLE_NAME:
        return False

    now = int(time.time())
    window_start = now - _WINDOW_SECONDS
    pk = _rate_limit_key(phone)

    try:
        table = boto3.resource("dynamodb").Table(_TABLE_NAME)
        resp = table.update_item(
            Key={"pk": pk, "sk": "count"},
            # `#ts` is the window's START, so it must survive every accepted
            # request. Overwriting it with `:now` would slide the window forward on
            # each message, so the condition below would never fail while a user
            # kept talking and the counter would become a lifetime total.
            # `if_not_exists` sets it once, when the item is created, and leaves it
            # alone until the window is retired below.
            UpdateExpression=(
                "SET #cnt = if_not_exists(#cnt, :zero) + :one, "
                "#ts = if_not_exists(#ts, :now), "
                "#ttl = if_not_exists(#ttl, :ttl)"
            ),
            ConditionExpression="attribute_not_exists(#ts) OR #ts >= :window_start",
            ExpressionAttributeNames={
                "#cnt": "msg_count",
                "#ts": "window_ts",
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":now": now,
                ":window_start": window_start,
                ":ttl": now + _WINDOW_SECONDS * 2,
            },
            ReturnValues="ALL_NEW",
        )
        count = resp["Attributes"].get("msg_count", 0)
        if count > _RATE_LIMIT_PER_MINUTE:
            logger.warning(
                "Rate limit exceeded for user %s (%d/%d in window)",
                pk,
                count,
                _RATE_LIMIT_PER_MINUTE,
            )
            return True
        return False

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Window expired — open a new one. Conditional on the window still
            # being stale: an unconditional put would let every invocation of a
            # simultaneous burst reset the count to 1 and pass. If another
            # invocation opened the window first, this put fails and we re-run the
            # increment against that window instead of clobbering it.
            try:
                table.put_item(
                    Item={
                        "pk": pk,
                        "sk": "count",
                        "msg_count": 1,
                        "window_ts": now,
                        "ttl": now + _WINDOW_SECONDS * 2,
                    },
                    ConditionExpression=(
                        "attribute_not_exists(#ts) OR #ts < :window_start"
                    ),
                    ExpressionAttributeNames={"#ts": "window_ts"},
                    ExpressionAttributeValues={":window_start": window_start},
                )
            except ClientError as reset_err:
                if (
                    reset_err.response["Error"]["Code"]
                    != "ConditionalCheckFailedException"
                ):
                    logger.debug("Rate limiter reset failed, allowing message")
                    return False
                return _count_against_open_window(table, pk, now)
            except Exception:
                logger.debug("Rate limiter reset failed, allowing message")
            return False
        logger.debug("Rate limiter DynamoDB error, allowing message: %s", e)
        return False
    except Exception:
        logger.debug("Rate limiter unexpected error, allowing message", exc_info=True)
        return False
