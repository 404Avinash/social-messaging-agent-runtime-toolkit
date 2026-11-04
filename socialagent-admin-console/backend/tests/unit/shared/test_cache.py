# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for shared.cache.TTLCache."""
from freezegun import freeze_time

from shared.cache import TTLCache


def test_set_and_get_within_ttl_returns_value():
    cache = TTLCache(ttl_seconds=30)
    with freeze_time("2026-05-08 12:00:00"):
        cache.set("key", "value")
        assert cache.get("key") == "value"


def test_get_after_ttl_returns_none():
    cache = TTLCache(ttl_seconds=30)
    with freeze_time("2026-05-08 12:00:00"):
        cache.set("key", "value")
    with freeze_time("2026-05-08 12:00:31"):
        assert cache.get("key") is None


def test_get_missing_key_returns_none():
    cache = TTLCache(ttl_seconds=30)
    assert cache.get("missing") is None


def test_set_overwrites_with_new_ttl():
    cache = TTLCache(ttl_seconds=30)
    with freeze_time("2026-05-08 12:00:00"):
        cache.set("key", "old")
    with freeze_time("2026-05-08 12:00:20"):
        cache.set("key", "new")
    with freeze_time("2026-05-08 12:00:45"):
        # 25s since second set → still valid
        assert cache.get("key") == "new"


def test_delete_removes_one_entry_and_tolerates_a_missing_key():
    cache = TTLCache(ttl_seconds=30)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.delete("a")
    cache.delete("never-set")
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_clear_removes_all_entries():
    cache = TTLCache(ttl_seconds=30)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None
