# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Rich WhatsApp message builder — framework agnostic.

Builds interactive message payloads for WhatsApp. Returns a dict with ``message``.
Uses ``socialagent_runtime.messages`` for payload construction.

Intended to be wrapped into an ``attach_interactive`` tool in your project's
tools/__init__.py. The agent's text output is the text message — no separate
send_message tool needed.

Note: ``call_button`` and ``address_request`` are intentionally NOT supported
here — WhatsApp silently drops both for standard WABA tiers, so exposing them
would let the agent build messages that never arrive.
"""

from __future__ import annotations

_SUPPORTED_TYPES = (
    "buttons",
    "list_menu",
    "link_button",
    "image",
    "video",
    "location",
    "location_request",
)


def _unescape(s: str) -> str:
    """Convert literal \\n, \\t, \\\\ in LLM-supplied strings to actual characters.

    LLMs occasionally emit tool args with escape sequences preserved rather
    than interpreted (artifact of the tool-calling JSON). This makes the
    escape sequences behave as if they were in a proper Python string.
    """
    if not s:
        return s
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _parse_rows(chunk: str, seen_ids: set[str] | None = None) -> list[dict]:
    """Parse ``row~desc;row~desc`` into WhatsApp list rows.

    Title and description are separated by ``~`` (never ``:`` — times contain
    colons). Rows are separated by ``;``. ``seen_ids`` holds the ids already
    used elsewhere in the message; pass the same set for every section.
    """
    rows = []
    if seen_ids is None:
        seen_ids = set()
    for item in chunk.split(";"):
        p = item.strip().split("~", 1) if "~" in item else [item.strip(), ""]
        title = p[0].strip()
        desc = p[1].strip()[:72] if len(p) > 1 else ""
        if not title:
            continue
        # WhatsApp caps a row title at 24 characters. Cutting it garbles the
        # row ("Parkview Apartment — $540,000" would read "Parkview Apartment —
        # $54") with no signal to anyone; refusing it sends the agent back with
        # the row named, so it shortens the title and retries.
        if len(title) > 24:
            raise ValueError(f"row title over 24 characters: {title!r}")
        # WhatsApp requires row ids unique across the whole message and rejects the
        # entire interactive if two collide — which they do when two rows share a
        # title (e.g. the same service at two times).
        base_id = title.lower().replace(" ", "_")
        row_id = base_id
        suffix = 2
        while row_id in seen_ids:
            row_id = f"{base_id}_{suffix}"
            suffix += 1
        seen_ids.add(row_id)
        rows.append({"id": row_id, "title": title, "description": desc})
    return rows


def build_rich_message(
    message_type: str,
    body: str = "",
    options: str = "",
    url: str = "",
    caption: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    name: str = "",
    address: str = "",
    button_text: str = "",
) -> dict:
    """Build a WhatsApp rich message payload.

    Args:
        message_type: One of: buttons, list_menu, link_button, image, video,
            location, location_request
        body: Message text. For image/video it is used as the caption when no
            explicit ``caption`` is given, so the agent's reply is not lost.
        options: For buttons: comma-separated labels ("Option A,Option B,Option C"). For list_menu: strict format "Section1|row1~desc;row2~desc|Section2|row3~desc" — pipe (|) separates sections, semicolon (;) separates rows within a section, tilde (~) separates row title from description. NEVER use comma between rows (use semicolon). NEVER use colon between title and description (use tilde).
        url: URL for media or link_button
        caption: Caption for media (overrides ``body`` for image/video)
        latitude: Latitude for location
        longitude: Longitude for location
        name: Place name for location
        address: Address string
        button_text: Button label for link_button
    Returns:
        {"message": {...}} on success,
        {"error": "..."} on failure.
    """
    # Import here to avoid circular deps — messages is in socialagent_runtime
    try:
        from socialagent_runtime import messages
    except ImportError:
        return {"error": "socialagent_runtime.messages not available"}

    # Unescape LLM-supplied text fields — the tool-calling JSON sometimes
    # preserves literal \n / \t instead of interpreting them. WhatsApp would
    # otherwise show "\n" as visible characters.
    body = _unescape(body)
    caption = _unescape(caption)
    address = _unescape(address)
    name = _unescape(name)
    button_text = _unescape(button_text)
    # For media, the agent's body IS the message — use it as the caption.
    media_caption = caption or body

    try:
        if message_type == "buttons":
            labels = [o.strip() for o in options.split(",") if o.strip()][:3]
            if len(labels) < 2:
                return {"error": "Need 2-3 comma-separated options"}
            # WhatsApp caps a button label at 20 characters; see _parse_rows.
            too_long = [o for o in labels if len(o) > 20]
            if too_long:
                return {"error": f"button label over 20 characters: {too_long[0]!r}"}
            # WhatsApp rejects the whole message when two button titles repeat.
            if len(set(labels)) < len(labels):
                return {"error": f"Button labels must be distinct, got {labels}"}
            msg = messages.buttons(body, labels)

        elif message_type == "list_menu":
            # Support pipe-separated sections: "Section1|row~desc;row~desc|Section2|row~desc"
            sections = []
            # Shared by every section: WhatsApp checks row ids across the whole
            # message, and the same time under two days shares a title.
            seen_ids: set[str] = set()
            if "|" in options:
                parts = options.split("|")
                i = 0
                while i < len(parts):
                    section_title = parts[i].strip()
                    rows = []
                    if i + 1 < len(parts) and (
                        "~" in parts[i + 1] or ";" in parts[i + 1]
                    ):
                        i += 1
                        rows = _parse_rows(parts[i], seen_ids)
                    if rows:
                        sections.append({"title": section_title[:24], "rows": rows})
                    i += 1
            else:
                rows = _parse_rows(options, seen_ids)
                if rows:
                    sections = [{"title": "Options", "rows": rows}]
            if not sections:
                return {
                    "error": "Need semicolon-separated items (title~description), optionally pipe-separated sections"
                }
            # Enforce WhatsApp's 10-row total limit, trimming from the end.
            while sum(len(s["rows"]) for s in sections) > 10:
                sections[-1]["rows"].pop()
                if not sections[-1]["rows"]:
                    sections.pop()
            # A tap reaches the agent as the row's title and description only,
            # joined by " — " and without its section, so two rows that read the
            # same (the same time under two days) are two choices it cannot tell
            # apart.
            seen_rows: set[str] = set()
            for section in sections:
                for row in section["rows"]:
                    reads = row["title"]
                    if row["description"]:
                        reads += f" — {row['description']}"
                    if reads.casefold() in seen_rows:
                        return {
                            "error": "List rows must differ in title or description,"
                            f" got two that read {reads!r}. The reply does not say"
                            " which section a row is in, so put the detail that"
                            " tells them apart (such as the day) in the row itself"
                        }
                    seen_rows.add(reads.casefold())
            msg = messages.list_menu(body, "View options", sections)

        elif message_type == "link_button":
            if not url:
                return {"error": "link_button requires a url"}
            msg = messages.cta_url(body, button_text or "Open", url)

        elif message_type == "image":
            if not url:
                return {"error": "image requires a url"}
            msg = messages.image(url, caption=media_caption)

        elif message_type == "video":
            if not url:
                return {"error": "video requires a url"}
            msg = messages.video(url, caption=media_caption)

        elif message_type == "location":
            # `is None`, not falsy: lat/lon of exactly 0 are valid (equator, prime
            # meridian) and `not 0` would reject them.
            if latitude is None or longitude is None:
                return {"error": "location requires both latitude and longitude"}
            msg = messages.location(latitude, longitude, name=name, address=address)

        elif message_type == "location_request":
            msg = messages.location_request(body)

        else:
            return {
                "error": f"Unknown message_type: {message_type}. Use: {', '.join(_SUPPORTED_TYPES)}"
            }

    except ValueError as e:
        return {"error": str(e)}

    return {"message": msg}
