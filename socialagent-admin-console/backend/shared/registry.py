# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""S3-backed registry with in-memory TTL cache.

Registry layout (S3):
    {REGISTRY_PREFIX}/{stack_id}/socialagent.yaml      — stack config
    {REGISTRY_PREFIX}/{stack_id}/tools.json     — tools metadata (optional)
    {REGISTRY_PREFIX}/{stack_id}/skills/*.md    — skill files (optional)

Cache semantics: container-local, TTL = REGISTRY_CACHE_TTL seconds.
Separate Lambda containers have separate caches — staleness after a write
is bounded by TTL.
"""
from __future__ import annotations

import yaml
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from shared.cache import TTLCache
from shared.clients import client
from shared.config import (
    DEFAULT_REGION,
    REGISTRY_BUCKET,
    REGISTRY_CACHE_TTL,
    REGISTRY_PREFIX,
)

logger = Logger(service="admin-console-backend", child=True)

_cache = TTLCache(ttl_seconds=REGISTRY_CACHE_TTL)

_KEY_LIST = "list_registry"

# A per-admin registry bucket (REGISTRY_PREFIX empty) logs its own S3 server
# access under this prefix, beside the stacks at its root
# (registry_access_log_prefix in infra-terraform/modules/api/main.tf). The
# listings skip it: it holds no socialagent.yaml.
ACCESS_LOG_PREFIX = "s3-access-logs/"


def template_prefix_for(config: dict) -> str:
    """Return the WhatsApp template prefix for a stack's registry config.

    Prefer the value the stack published. Older registries predate that field, so
    fall back to reconstructing it exactly as Terraform does in
    ``infra-terraform/locals.tf:22``::

        "${var.environment}_${replace(var.project_name, "-", "_")}_"

    The hyphen replacement is the part that matters: without it, a project named
    ``my-bot`` yields ``dev_my-bot_``, which no registered template name starts
    with, so template listings come back empty and prefix stripping on send is a
    no-op — leaving the provider to prepend its own prefix a second time.

    Returns ``""`` for a config carrying neither field, so a missing prefix reads
    as absent rather than as the everything-matching ``"_"``.
    """
    published = config.get("template_prefix")
    if published:
        return published
    environment = config.get("environment", "")
    project = config.get("project", "")
    if not (environment and project):
        return ""
    return f"{environment}_{project.replace('-', '_')}_"


def template_base_prefix_for(config: dict) -> str:
    """Return the project-only template prefix (e.g. ``"my_bot_"``).

    Used as a secondary match/strip for templates registered without the
    environment segment. Carries the same hyphen replacement as
    :func:`template_prefix_for` — a raw ``"my-bot_"`` matches nothing, because
    Meta only ever saw the underscored form.
    """
    project = config.get("project", "")
    return f"{project.replace('-', '_')}_" if project else ""


def template_matches_stack(name: str, config: dict) -> bool:
    """Whether the WABA template *name* is one of this stack's.

    It is when it starts with the stack's prefix or the project-only one. The
    template list and the health check both decide with this, so they match a
    stack's template names the same way. An empty prefix, from a config missing
    the fields it is built from, matches nothing, not every template in the
    WABA.
    """
    return any(
        prefix and name.startswith(prefix)
        for prefix in (template_prefix_for(config), template_base_prefix_for(config))
    )


def _registry_key(relative: str) -> str:
    """Return the absolute S3 key for a registry object, applying prefix."""
    relative = relative.lstrip("/")
    return f"{REGISTRY_PREFIX}/{relative}" if REGISTRY_PREFIX else relative


def _registry_prefix_for(relative: str) -> str:
    """Prefix form for ListObjects calls."""
    if not relative:
        return f"{REGISTRY_PREFIX}/" if REGISTRY_PREFIX else ""
    return _registry_key(relative)


def list_registry() -> list[dict]:
    """Return all stack configs. Cached for REGISTRY_CACHE_TTL seconds."""
    cached = _cache.get(_KEY_LIST)
    if cached is not None:
        return cached

    s3 = client("s3", DEFAULT_REGION)
    stacks: list[dict] = []
    resp = s3.list_objects_v2(
        Bucket=REGISTRY_BUCKET,
        Prefix=_registry_prefix_for(""),
        Delimiter="/",
    )
    for prefix in resp.get("CommonPrefixes", []):
        if prefix["Prefix"] == ACCESS_LOG_PREFIX:
            continue
        key = prefix["Prefix"] + "socialagent.yaml"
        try:
            obj = s3.get_object(Bucket=REGISTRY_BUCKET, Key=key)
            config = yaml.safe_load(obj["Body"].read())
            if config:
                stacks.append(config)
        except Exception:
            # One bad stack shouldn't break list_registry for others, but log
            # with enough context (key + exception) for operator triage.
            logger.exception("list_registry_load_failed", extra={"key": key})
            continue

    _cache.set(_KEY_LIST, stacks)
    return stacks


def get_registry(stack_id: str) -> dict | None:
    """Return a single stack config. Cached per stack_id."""
    cached = _cache.get(stack_id)
    if cached is not None:
        return cached

    s3 = client("s3", DEFAULT_REGION)
    key = _registry_key(f"{stack_id}/socialagent.yaml")
    try:
        obj = s3.get_object(Bucket=REGISTRY_BUCKET, Key=key)
        config = yaml.safe_load(obj["Body"].read())
        _cache.set(stack_id, config)
        return config
    except s3.exceptions.NoSuchKey:
        return None


def put_registry(stack_id: str, config: dict) -> bool:
    """Write a stack config back. Invalidates cache for this stack_id + list."""
    s3 = client("s3", DEFAULT_REGION)
    try:
        s3.put_object(
            Bucket=REGISTRY_BUCKET,
            Key=_registry_key(f"{stack_id}/socialagent.yaml"),
            Body=yaml.dump(config, default_flow_style=False),
        )
        # Invalidate local cache so next read picks up the change.
        # Other containers still have stale cache until their TTL expires.
        _cache.clear()
        return True
    except Exception:
        logger.exception("put_registry_failed", extra={"stack_id": stack_id})
        return False


_KEY_ALL = "all_registries"


def read_all_registries() -> tuple[dict[str, dict], list[str]]:
    """Every stack config keyed by stack_id, read now, and the stack ids whose
    config could not be read.

    ``get_all_registries`` skips a config it cannot read, answers ``{}`` when
    the listing fails, and caches either, which suits a page that lists what
    it can. A change that must account for every registered stack, such as a
    phone bind that decides whether a WABA is shared, reads with this
    instead: it raises when the listing fails, lists each config it could not
    read, and neither reads nor fills the cache.

    A prefix that holds no socialagent.yaml (``NoSuchKey``) is no stack, as
    ``get_registry`` (``None``) and ``list_registry`` (skipped) already treat
    it, so it is left out and not listed as unread. Any other failed read,
    such as an access denial, may hide a stack, so it is listed.

    It reads every page of the listing (up to 1000 prefixes a page), and
    raises on a page that says there is more but gives no token to read it
    with.
    """
    s3 = client("s3", DEFAULT_REGION)
    stacks: dict[str, dict] = {}
    unread: list[str] = []
    for raw in _all_stack_prefixes(s3):
        if raw == ACCESS_LOG_PREFIX:
            continue
        if REGISTRY_PREFIX and raw.startswith(f"{REGISTRY_PREFIX}/"):
            raw = raw[len(REGISTRY_PREFIX) + 1:]
        stack_id = raw.rstrip("/")
        try:
            obj = s3.get_object(Bucket=REGISTRY_BUCKET, Key=_registry_key(f"{stack_id}/socialagent.yaml"))
            config = yaml.safe_load(obj["Body"].read())
        except Exception as e:
            if _no_such_key(e):
                continue
            logger.exception("get_all_registries_load_failed", extra={"stack_id": stack_id})
            unread.append(stack_id)
            continue
        if config:
            stacks[stack_id] = config
    return stacks, unread


def _all_stack_prefixes(s3) -> list[str]:
    """Every prefix one level under the registry root, across list pages."""
    kwargs = {"Bucket": REGISTRY_BUCKET, "Prefix": _registry_prefix_for(""), "Delimiter": "/"}
    prefixes: list[str] = []
    while True:
        resp = s3.list_objects_v2(**kwargs)
        prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
        if not resp.get("IsTruncated"):
            return prefixes
        token = resp.get("NextContinuationToken")
        if not token:
            raise RuntimeError("registry listing is truncated and gives no continuation token")
        kwargs["ContinuationToken"] = token


def _no_such_key(exc: Exception) -> bool:
    """Whether *exc* is S3 saying the object does not exist."""
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "NoSuchKey"


def get_all_registries() -> dict[str, dict]:
    """Return all stack configs keyed by stack_id. Cached for REGISTRY_CACHE_TTL.

    A config that cannot be read is left out, and a failed listing gives
    ``{}``; ``read_all_registries`` reports both.
    """
    cached = _cache.get(_KEY_ALL)
    if cached is not None:
        return cached

    stacks: dict[str, dict] = {}
    try:
        stacks, _unread = read_all_registries()
    except Exception:
        logger.exception("get_all_registries_failed")

    _cache.set(_KEY_ALL, stacks)
    return stacks


def _clear_registry_cache() -> None:
    """Test helper — clear cache between tests."""
    _cache.clear()
