# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""TTL-based in-memory cache, for Lambda container reuse.

Thread-safe for CPython due to GIL; no explicit lock needed for the simple
get/set pattern. Values expire based on wall-clock time at insertion.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any


class TTLCache:
    """Simple time-to-live cache.

    Not intended for high-cardinality or long-lived data; designed for small
    sets (≤100 entries) of registry/config reads within a Lambda container.
    """

    def __init__(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[datetime, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if datetime.now(timezone.utc) >= expires_at:
            del self._store[key]
            return None
        # Return a deep copy so a caller mutating the result (e.g. a route doing
        # config["phone_number_id"] = ... before put_registry) cannot corrupt the
        # cached object that other reads in the same warm container share.
        return copy.deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        # Store a copy too, so a later mutation of the caller's object doesn't
        # retroactively change what's cached.
        self._store[key] = (expires_at, copy.deepcopy(value))

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
