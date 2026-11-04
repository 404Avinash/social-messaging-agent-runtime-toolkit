# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every ADR has the same shape, so tooling can parse the set and a reader
always finds the same sections in the same order.
"""

import re
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).parent.parent / "docs" / "adr"
RECORDS = sorted(
    p
    for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")
    if not p.name.startswith("0000")
)
SECTIONS = [
    "Context",
    "Decision",
    "Alternatives considered",
    "Consequences",
    "Open questions",
    "Enforced by",
]
STATUS = re.compile(r"^(Proposed|Accepted|Superseded by ADR-\d{4})$")
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _front_matter(text: str) -> dict:
    m = FRONT.match(text)
    assert m, "front matter block missing"
    fields = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def test_there_are_records():
    assert len(RECORDS) >= 8


@pytest.mark.parametrize("record", RECORDS, ids=lambda p: p.name)
def test_record_has_the_standard_shape(record):
    text = record.read_text(encoding="utf-8")
    fm = _front_matter(text)
    number = record.name[:4]
    assert fm["id"] == f"ADR-{number}", fm.get("id")
    for key in (
        "title",
        "status",
        "date",
        "deciders",
        "supersedes",
        "superseded_by",
        "tags",
    ):
        assert key in fm, f"front matter missing {key}"
    assert STATUS.match(fm["status"]), fm["status"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm["date"]), fm["date"]
    assert f"# ADR-{number}: {fm['title']}" in text, (
        "H1 does not match front matter title"
    )
    # the human-readable table repeats the front matter
    assert f"| {fm['status']} | {fm['date']} | {fm['deciders']} |" in text, (
        "status table disagrees with front matter"
    )
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings == SECTIONS, headings
    if fm["status"] == "Accepted":
        enforced = text.split("## Enforced by", 1)[1]
        assert "`" in enforced, (
            "an Accepted record names the code or tests that enforce it"
        )


def test_index_lists_every_record():
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for record in RECORDS:
        assert f"]({record.name})" in index, (
            f"{record.name} missing from docs/adr/README.md"
        )


def test_cited_paths_and_symbols_exist():
    """Every `path` and `path::symbol` an ADR cites in Enforced-by exists.

    A renamed test would otherwise leave the record pointing at nothing while
    staying green; the symbol check reads the file for a `def` or `class`."""
    root = ADR_DIR.parent.parent
    missing = []
    for record in RECORDS:
        enforced = record.read_text(encoding="utf-8").split("## Enforced by", 1)[1]
        for cited in re.findall(
            r"`((?:src|tests|packages|infra-terraform|docs|recipes|blueprints)/[^`]+)`",
            enforced,
        ):
            path, _, symbols = cited.partition("::")
            if any(ch in path for ch in "*<>"):  # a glob or a placeholder, not one path
                continue
            target = root / path
            if not target.exists():
                missing.append(f"{record.name}: {path}")
                continue
            if symbols and target.is_file():
                source = target.read_text(encoding="utf-8")
                for sym in symbols.split("::"):
                    sym = sym.strip("` ").split(".")[
                        -1
                    ]  # Class.method cites the method
                    if not re.search(
                        rf"^\s*(async\s+def|def|class)\s+{re.escape(sym)}\b",
                        source,
                        re.M,
                    ):
                        missing.append(f"{record.name}: {path}::{sym}")
    assert not missing, missing


def test_index_statuses_match_front_matter():
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for record in RECORDS:
        fm = _front_matter(record.read_text(encoding="utf-8"))
        row = next((ln for ln in index.splitlines() if f"]({record.name})" in ln), None)
        assert row, f"{record.name} not in the index"
        assert f"| {fm['status']} |" in row, (
            f"{record.name}: index status differs from front matter ({fm['status']})"
        )
