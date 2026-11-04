# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for template sync orchestrator.
"""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialagent.templates.sync import sync_templates

_YAML = textwrap.dedent("""\
    templates:
      - name: welcome
        category: UTILITY
        language: es_MX
        components:
          - type: BODY
            text: "Bienvenido {{1}}."
            variables:
              - position: 1
                name: name
      - name: reminder
        category: UTILITY
        language: es_MX
        components:
          - type: BODY
            text: "Recuerda tu cita el {{1}}."
            variables:
              - position: 1
                name: date
""")


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    p = tmp_path / "templates.yaml"
    p.write_text(_YAML)
    return p


def _make_provider(registered: list[dict]) -> MagicMock:
    provider = MagicMock()
    provider.list_registered_templates = AsyncMock(return_value=registered)
    provider.register_template = AsyncMock()
    provider.delete_template = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_creates_new_templates(yaml_path: Path) -> None:
    provider = _make_provider([])
    result = await sync_templates(yaml_path, provider)
    assert set(result["created"]) == {"welcome", "reminder"}
    assert result["updated"] == []
    assert provider.register_template.call_count == 2


@pytest.mark.asyncio
async def test_skips_already_registered_by_default(yaml_path: Path) -> None:
    """Registered templates are skipped — provider listings have no body to diff."""
    existing = [{"name": "welcome"}]
    provider = _make_provider(existing)
    result = await sync_templates(yaml_path, provider)
    assert "welcome" in result["skipped"]
    assert "welcome" not in result["updated"]
    assert "welcome" not in result["created"]
    assert "reminder" in result["created"]
    # Only the missing template is registered, never the existing one.
    provider.register_template.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_reregisters_existing(yaml_path: Path) -> None:
    """force=True re-registers already-registered templates (push body changes)."""
    existing = [{"name": "welcome"}]
    provider = _make_provider(existing)
    result = await sync_templates(yaml_path, provider, force=True)
    assert "welcome" in result["updated"]
    assert "welcome" not in result["skipped"]
    assert "reminder" in result["created"]
    assert provider.register_template.call_count == 2


@pytest.mark.asyncio
async def test_skips_unchanged_template(yaml_path: Path) -> None:
    existing = [{"name": "welcome"}, {"name": "reminder"}]
    provider = _make_provider(existing)
    result = await sync_templates(yaml_path, provider)
    assert set(result["skipped"]) == {"welcome", "reminder"}
    assert result["created"] == []
    assert result["updated"] == []
    provider.register_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_deletes_removed_when_flag_set(yaml_path: Path) -> None:
    existing = [{"name": "obsolete"}]
    provider = _make_provider(existing)
    result = await sync_templates(
        yaml_path, provider, prefix="prod_", delete_removed=True
    )
    assert "obsolete" in result["deleted"]
    provider.delete_template.assert_called_once_with("obsolete")


@pytest.mark.asyncio
async def test_no_delete_by_default(yaml_path: Path) -> None:
    existing = [{"name": "obsolete"}]
    provider = _make_provider(existing)
    result = await sync_templates(yaml_path, provider)
    assert result["deleted"] == []
    provider.delete_template.assert_not_called()


@pytest.mark.asyncio
async def test_prefix_applied(yaml_path: Path) -> None:
    provider = _make_provider([])
    result = await sync_templates(yaml_path, provider, prefix="prod_")
    assert all(n.startswith("prod_") for n in result["created"])


@pytest.mark.asyncio
async def test_rejected_template_is_reported_as_failed(yaml_path: Path) -> None:
    """A provider rejection lands in "failed" instead of vanishing into the log.

    Meta rejects individual templates (wrong category, too many variables for the
    body length). Sync keeps going, so without this bucket the caller has no way
    to tell a clean run from one where nothing was registered.
    """
    provider = _make_provider([])
    provider.register_template = AsyncMock(
        side_effect=RuntimeError("InvalidParametersException")
    )
    result = await sync_templates(yaml_path, provider)
    assert set(result["failed"]) == {"welcome", "reminder"}
    assert result["created"] == []


@pytest.mark.asyncio
async def test_one_rejection_does_not_block_the_rest(yaml_path: Path) -> None:
    provider = _make_provider([])
    provider.register_template = AsyncMock(side_effect=[RuntimeError("rejected"), None])
    result = await sync_templates(yaml_path, provider)
    assert len(result["failed"]) == 1
    assert len(result["created"]) == 1


@pytest.mark.asyncio
async def test_delete_removed_without_prefix_refuses(yaml_path: Path) -> None:
    """An unprefixed --delete-removed would delete the whole WABA.

    With no prefix, ``list_registered_templates("")`` returns every template in
    the linked WhatsApp Business Account, while the local set holds bare YAML
    names. Nothing matches, so every template — including other projects'
    sharing the WABA — would be deleted and need full Meta re-approval. There is
    no way to tell "not mine" from "not in my YAML" without a prefix, so refuse.
    """
    provider = _make_provider([{"name": "dev_otherproject_welcome"}])

    with pytest.raises(ValueError, match="prefix"):
        await sync_templates(yaml_path, provider, prefix="", delete_removed=True)

    provider.delete_template.assert_not_called()


@pytest.mark.asyncio
async def test_delete_removed_without_prefix_refuses_before_creating(
    yaml_path: Path,
) -> None:
    """The refusal happens up front, not halfway through a sync.

    Failing after some templates were registered with Meta would leave the sync
    half-applied, so the guard runs before any provider mutation.
    """
    provider = _make_provider([])

    with pytest.raises(ValueError, match="prefix"):
        await sync_templates(yaml_path, provider, delete_removed=True)

    provider.register_template.assert_not_called()
    provider.delete_template.assert_not_called()


@pytest.mark.asyncio
async def test_delete_failure_does_not_abort_remaining_deletes(
    yaml_path: Path,
) -> None:
    """One failed delete must not discard the rest of the run.

    The create loop already tolerates a single failure; the delete loop did not,
    so a mid-loop error aborted the sync and threw away the result dict.
    """
    provider = _make_provider([{"name": "gone_first"}, {"name": "gone_second"}])
    provider.delete_template = AsyncMock(
        side_effect=[RuntimeError("Meta rejected"), None]
    )

    result = await sync_templates(
        yaml_path, provider, prefix="prod_", delete_removed=True
    )

    assert provider.delete_template.call_count == 2
    # The failed one is not reported as deleted; the successful one is.
    assert len(result["deleted"]) == 1
