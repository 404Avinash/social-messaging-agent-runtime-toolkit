# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property listings and dynamically generated viewing slots."""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta

# The agency the user is talking to. Names and places are fictional.
AGENCY_NAME = "Example Realty"
AGENCY_ADDRESS = f"{AGENCY_NAME} — 100 Market St, Your City"

# Each listing is a place the user goes to see, so it carries the address and
# the coordinates the reminder's location pin uses.
LISTINGS = [
    {
        "id": "downtown-loft",
        "name": "Downtown Loft",
        "price": "$420,000",
        "address": "12 River St, Your City",
        "lat": 19.4204,  # dummy points: open parkland, no business at them
        "lng": -99.1914,
    },
    {
        "id": "garden-house",
        "name": "Garden House",
        "price": "$680,000",
        "address": "48 Elm Ave, Your City",
        "lat": 19.4178,
        "lng": -99.1838,
    },
    {
        "id": "parkview-apartment",
        "name": "Parkview Apartment",
        "price": "$540,000",
        "address": "7 Park Rd, Your City",
        "lat": 19.2896,
        "lng": -99.1971,
    },
]

# Hours available for viewings (9 AM – 6 PM)
_HOURS = [9, 10, 11, 12, 13, 14, 15, 16, 17]


def _generate_slots(seed_extra: str = "") -> list[dict]:
    """Generate 2-3 random slots per day across the next 7 days, between 9 AM and 6 PM."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rng = random.Random(
        f"{today.toordinal()}-{seed_extra}"
    )  # deterministic per day+listing
    slots = []
    for i in range(7):
        day = today + timedelta(days=i + 1)
        # %-d/%-I (no leading zero) are glibc/BSD extensions, not supported by
        # Python's strftime on Windows — use %d/%I and strip a leading zero
        # instead, so this also works for local dev on Windows.
        day_name = day.strftime("%A, %B ") + str(day.day)
        day_prefix = day.strftime("%a").lower()
        hours = sorted(rng.sample(_HOURS, rng.randint(2, 3)))
        for hour in hours:
            slot_id = f"{day_prefix}_{hour:02d}"
            hour12 = day.replace(hour=hour).strftime("%I:%M %p").lstrip("0")
            slots.append({"id": slot_id, "date": day_name, "time": hour12})
    return slots


# SLOTS is regenerated per calendar day rather than once at module import: the
# slots are seeded off "today", but an AgentCore Runtime warm container can
# stay alive across midnight (up to ~15 min idle), and a module-level constant
# computed once at import time would keep showing yesterday's slot dates to
# every user for the rest of that container's life. _slots_for_today() caches
# by date so repeated calls within the same day are still O(1) after the
# first, but a new day gets a fresh set automatically without a restart.
_slots_cache: dict[str, tuple[str, dict[str, list[dict]]]] = {}


def _slots_for_today() -> dict[str, list[dict]]:
    today_key = date.today().isoformat()
    cached_day, cached_slots = _slots_cache.get("_", (None, None))
    if cached_day == today_key:
        return cached_slots
    fresh = {p["id"]: _generate_slots(p["id"]) for p in LISTINGS}
    _slots_cache["_"] = (today_key, fresh)
    return fresh


class _SlotsProxy:
    """Dict-like view over the current day's slots, regenerated on access.

    Exists so ``from src.catalog import SLOTS`` (and any code that already
    does ``SLOTS[property_id]`` / ``SLOTS.get(property_id)``) keeps working
    without changes elsewhere, while every access transparently re-checks
    whether the calendar day has rolled over.
    """

    def __getitem__(self, property_id: str) -> list[dict]:
        return _slots_for_today()[property_id]

    def get(self, property_id: str, default=None):
        return _slots_for_today().get(property_id, default)

    def __contains__(self, property_id: str) -> bool:
        return property_id in _slots_for_today()

    def __iter__(self):
        return iter(_slots_for_today())


SLOTS = _SlotsProxy()


def format_catalog() -> str:
    """Return a formatted string of the listings for the system prompt (no IDs)."""
    return "\n".join(f"- {p['name']} — {p['price']}" for p in LISTINGS)


def find_slot(property_id: str, slot_id: str) -> dict | None:
    """Return the slot dict for a given listing and slot ID, or None."""
    return next((s for s in SLOTS.get(property_id, []) if s["id"] == slot_id), None)


def find_property(property_id: str) -> dict | None:
    """Return the listing dict for a given ID, or None."""
    return next((p for p in LISTINGS if p["id"] == property_id), None)


def resolve_property_by_name(name: str) -> dict | None:
    """Find a listing by (partial, case-insensitive) name match."""
    name_lower = name.lower()
    return next((p for p in LISTINGS if name_lower in p["name"].lower()), None)


def _hour_24(time_str: str) -> int | None:
    """Extract the hour as 0-23 from a loose time string, or None.

    Handles "4:00 PM", "4 PM", "16:00", "16h" across languages — the agent
    converses in the user's language and may not echo the catalog's exact
    "H:MM AM/PM" formatting, so match on the numeric hour instead of the string.
    """
    m = re.search(r"(\d{1,2})", time_str)
    if not m:
        return None
    hour = int(m.group(1))
    low = time_str.lower()
    if "pm" in low and hour < 12:
        hour += 12
    elif "am" in low and hour == 12:
        hour = 0
    return hour if 0 <= hour <= 23 else None


def resolve_slot_by_datetime(property_id: str, date: str, time: str) -> dict | None:
    """Find a slot by listing ID matching the day-of-month and hour numerically.

    Language- and format-robust: the day number (e.g. 17) and the hour are
    identical whether the agent says "Friday, July 17 at 4:00 PM" or
    "viernes 17 de julio a las 16:00", so we compare those numbers rather than
    requiring the exact catalog string (which would fail on any translation or
    time-format variation).
    """
    day_match = re.search(r"\b(\d{1,2})\b", date)
    day = day_match.group(1) if day_match else None
    want_hour = _hour_24(time)
    # Require BOTH the day and the hour to parse. A one-sided match is ambiguous
    # (a day has several slots; an hour repeats across days) and would silently
    # book the first candidate — better to fail so the agent re-asks the user.
    if day is None or want_hour is None:
        return None
    return next(
        (
            s
            for s in SLOTS.get(property_id, [])
            if re.search(rf"\b{day}\b", s["date"]) and _hour_24(s["time"]) == want_hour
        ),
        None,
    )


def format_slots(property_id: str) -> str:
    """Return formatted slot list for a listing (no IDs)."""
    return "\n".join(
        f"- {s['date']} at {s['time']}" for s in SLOTS.get(property_id, [])
    )
