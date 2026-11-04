# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``compare_and_set`` must never report a value it did not store.

The contract, per docs/state.md, is "returns whichever value ends up stored" —
so returning the caller's own value after a failed conditional write is the one
thing it must not do.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import state as state_mod  # noqa: E402

PHONE = "+5215555550100"
KEY = "current_operation_id"

_CONDITION_FAILED = ClientError(
    {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
)


@pytest.fixture
def table():
    """A stubbed DynamoDB table whose conditional put always loses the race."""
    tbl = MagicMock()
    tbl.put_item.side_effect = _CONDITION_FAILED
    with patch.object(state_mod, "_get_table", return_value=tbl):
        yield tbl


@pytest.mark.asyncio
async def test_absent_item_does_not_report_our_value_as_stored(table):
    """The item vanished (TTL, or an earlier delete), so nothing was written.

    `expected` cannot match an absent item, so the condition fails; the read-back
    then finds nothing. Returning the caller's value here told a rotate-the-id
    caller that `op-2` was committed, so it wrote child records under an operation
    id that did not exist — the next turn read `current_operation_id` as None and
    the batch was orphaned. No concurrency required: one caller after a TTL expiry
    is enough.
    """
    table.get_item.return_value = {}

    got = await state_mod.compare_and_set(
        PHONE, KEY, expected="op-1", factory=lambda: "op-2"
    )

    assert got is None


@pytest.mark.asyncio
async def test_item_without_data_does_not_report_our_value(table):
    """An item held by a foreign writer stores nothing comparable either.

    `atomic_increment` writes pk/sk/n/ttl and no `data`, so a counter key hits
    this path.
    """
    table.get_item.return_value = {"Item": {"pk": PHONE, "sk": KEY, "n": 7}}

    got = await state_mod.compare_and_set(
        PHONE, KEY, expected="op-1", factory=lambda: "op-2"
    )

    assert got is None


@pytest.mark.asyncio
async def test_loser_returns_the_winners_value(table):
    """Losing the race must converge on whatever the winner actually stored."""
    table.get_item.return_value = {"Item": {"data": json.dumps("op-winner")}}

    got = await state_mod.compare_and_set(
        PHONE, KEY, expected=None, factory=lambda: "op-mine"
    )

    assert got == "op-winner"


@pytest.mark.asyncio
async def test_read_back_is_strongly_consistent(table):
    """The read that decides the return value needs ConsistentRead.

    DynamoDB only guarantees read-your-writes for strongly consistent reads. An
    eventually-consistent read can miss the winner's write and hand back a stale
    value — or nothing — so two racers end up on different operation ids, which is
    the split-batch bug this function exists to prevent.
    """
    table.get_item.return_value = {"Item": {"data": json.dumps("op-winner")}}

    await state_mod.compare_and_set(
        PHONE, KEY, expected=None, factory=lambda: "op-mine"
    )

    assert table.get_item.call_args.kwargs.get("ConsistentRead") is True


@pytest.mark.asyncio
async def test_successful_write_returns_the_new_value(table):
    """The winning path is unchanged."""
    table.put_item.side_effect = None

    got = await state_mod.compare_and_set(
        PHONE, KEY, expected=None, factory=lambda: "op-mine"
    )

    assert got == "op-mine"


@pytest.mark.asyncio
async def test_non_conditional_errors_still_propagate(table):
    """A throttle or validation error must not be mistaken for a lost race."""
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
    )

    with pytest.raises(ClientError):
        await state_mod.compare_and_set(
            PHONE, KEY, expected=None, factory=lambda: "op-mine"
        )


# ── reads must not crash on items that carry no `data` attribute ───────────────


@pytest.mark.asyncio
async def test_get_returns_none_for_item_without_data(table):
    """A counter item (pk/sk/n/ttl, no `data`) must read as None, not KeyError.

    atomic_increment writes such items, and list_keys does not hide the counter#
    namespace, so the agent can be handed a key whose get() then crashed the turn.
    """
    table.get_item.return_value = {
        "Item": {"pk": PHONE, "sk": "counter#photo_seq", "n": 3, "ttl": 1}
    }

    assert await state_mod.get(PHONE, "counter#photo_seq") is None


# ── dev/prod parity for expected=None against a stored null ────────────────────


@pytest.fixture
def dev_store(monkeypatch, tmp_path):
    """Route the dev-mode store to a temp file and force dev mode (no table)."""
    monkeypatch.setattr(state_mod, "_DEV_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "_get_table", lambda: None)


@pytest.mark.asyncio
async def test_dev_expected_none_does_not_overwrite_stored_null(dev_store):
    """A key holding None is present, so expected=None must NOT write.

    Prod maps expected=None to attribute_not_exists(sk); dev used a plain
    inequality that could not tell absent from stored-None, so this call wrote in
    dev and no-opped in prod — a mint that worked under `socialagent dev` and silently
    did nothing in production.
    """
    await state_mod.put(PHONE, "k", None)

    got = await state_mod.compare_and_set(
        PHONE, "k", expected=None, factory=lambda: "minted"
    )

    assert got is None
    assert await state_mod.get(PHONE, "k") is None


@pytest.mark.asyncio
async def test_dev_expected_none_writes_when_truly_absent(dev_store):
    """The intended path still works: absent key, expected=None → write."""
    got = await state_mod.compare_and_set(
        PHONE, "fresh", expected=None, factory=lambda: "minted"
    )

    assert got == "minted"
    assert await state_mod.get(PHONE, "fresh") == "minted"
