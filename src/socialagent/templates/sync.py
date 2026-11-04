# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Template sync orchestrator: reconciles local YAML templates against the
provider, creating missing ones and (optionally) deleting removed ones.

Provider-specific logic lives entirely in the provider class. This module is
a thin orchestrator — adding a new provider requires zero changes here.

Already-registered templates are skipped by default: provider listings expose
only metadata (no body text), so a body-level diff is not possible and
re-registering would needlessly re-trigger Meta review. Pass ``force=True`` to
re-register every local template regardless.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from socialagent.providers import MessageProvider
from socialagent.templates.registry import TemplateRegistry

logger = logging.getLogger(__name__)


async def sync_templates(
    templates_path: str | Path,
    provider: MessageProvider,
    prefix: str = "",
    delete_removed: bool = False,
    force: bool = False,
    before_send: Callable[[list[str]], None] | None = None,
    confirm_delete: Callable[[list[str]], bool] | None = None,
) -> dict[str, list[str]]:
    """Sync local YAML templates to the provider.

    Reconciles local template definitions against what is currently registered
    with the provider:

    - **Create**: templates in YAML but not registered.
    - **Update**: templates in both, only when ``force=True`` (see below).
    - **Skip**: templates already registered (the default — no body diff).
    - **Delete**: templates registered but not in YAML (only when
      ``delete_removed=True``).

    .. note::
       Provider template listings expose only metadata (name, status,
       language, category), **not** the rendered body text — the EUM Social
       ``list_whatsapp_message_templates`` API returns no body. A body-level
       diff is therefore impossible from the listing alone, so an
       already-registered template is **skipped** rather than re-registered.
       This keeps sync idempotent and avoids re-triggering Meta template
       review (which can reclassify UTILITY templates as MARKETING).

       To push a changed body, pass ``force=True`` to re-register every local
       template regardless of current state.

    Args:
        templates_path: Path to the templates YAML file.
        provider: Messaging provider to sync against.
        prefix: Name prefix applied to all templates (e.g. ``"prod_myapp_"``).
        delete_removed: If ``True``, delete provider templates not in YAML.
        force: If ``True``, re-register templates that already exist (reported
            under ``"updated"``). If ``False`` (default), they are skipped.
        before_send: Called with the YAML names about to be sent to the provider
            (new ones, plus existing ones when ``force``) before any of them is.
            Raising aborts the sync with nothing registered, so a caller can
            gate on validating exactly what will be sent — an already-approved
            template that is skipped is never judged again.
        confirm_delete: Called with the registered names ``delete_removed``
            would delete; nothing is deleted unless it returns ``True``. Names
            are matched by prefix, so they can include another project's
            templates whose prefix starts with this one (``dev_bot_`` matches
            ``dev_bot_v2_*``). ``None`` deletes without asking.

    Returns:
        A dict with keys ``"created"``, ``"updated"``, ``"deleted"``,
        ``"skipped"``, ``"failed"`` mapping to lists of template names in each
        bucket. One rejected template does not abort the others, so callers that
        need an exit status must check ``"failed"`` — see ``cmd_templates_sync``.
    """
    if delete_removed and not prefix:
        # With no prefix, list_registered_templates("") returns every template in
        # the linked WABA while the local set holds bare YAML names, so nothing
        # matches and the delete loop wipes the account — other projects sharing
        # the WABA included, each needing full Meta re-approval. "Not mine" and
        # "not in my YAML" are indistinguishable without a prefix. Refuse before
        # touching the provider, so a rejected sync is never half-applied.
        raise ValueError(
            "delete_removed requires a non-empty prefix: without one, every "
            "template in the WhatsApp Business Account would be deleted, "
            "including other projects'. Pass --prefix or set TEMPLATE_PREFIX."
        )

    registry = TemplateRegistry(templates_path)
    registered = {t["name"] for t in await provider.list_registered_templates(prefix)}

    result: dict[str, list[str]] = {
        "created": [],
        "updated": [],
        "deleted": [],
        "skipped": [],
        "failed": [],
    }

    to_send = [n for n in registry.names if force or f"{prefix}{n}" not in registered]
    if before_send is not None and to_send:
        before_send(to_send)

    for name in registry.names:
        tmpl = registry.get(name)
        prefixed_name = f"{prefix}{name}"
        try:
            if prefixed_name not in registered:
                await provider.register_template(tmpl, prefix)
                result["created"].append(prefixed_name)
                logger.info("Created template: %s", prefixed_name)
            elif force:
                await provider.register_template(tmpl, prefix)
                result["updated"].append(prefixed_name)
                logger.info("Re-registered template (force): %s", prefixed_name)
            else:
                result["skipped"].append(prefixed_name)
                logger.info("Template already registered, skipping: %s", prefixed_name)
        except Exception:
            # Carry on with the remaining templates — one rejection should not
            # leave the rest unregistered — but record it so the caller can fail.
            result["failed"].append(prefixed_name)
            logger.exception("Failed to sync template %s", prefixed_name)

    if delete_removed:
        local_prefixed = {f"{prefix}{n}" for n in registry.names}
        to_delete = sorted(n for n in registered if n not in local_prefixed)
        if to_delete and (confirm_delete is None or confirm_delete(to_delete)):
            for name in to_delete:
                # Tolerate a single failure like the create loop above, so one
                # rejected delete does not abort the run and discard the result.
                try:
                    await provider.delete_template(name)
                except Exception:
                    logger.exception("Failed to delete template %s", name)
                    continue
                result["deleted"].append(name)
                logger.info("Deleted template: %s", name)

    return result
