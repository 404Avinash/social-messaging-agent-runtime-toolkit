# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for shared.registry — S3-backed with TTL cache."""
import boto3
import pytest
import yaml
from freezegun import freeze_time
from moto import mock_aws


@pytest.fixture
def registry_bucket():
    """Create the registry S3 bucket with 2 sample stacks."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-registry")

        s3.put_object(
            Bucket="test-registry",
            Key="socialagent-dev/socialagent.yaml",
            Body=yaml.dump({
                "project": "socialagent",
                "environment": "dev",
                "region": "us-east-1",
                "outputs": {"webhook_lambda": "dev-socialagent-webhook"},
            }),
        )
        s3.put_object(
            Bucket="test-registry",
            Key="scheduler-dev/socialagent.yaml",
            Body=yaml.dump({
                "project": "scheduler",
                "environment": "dev",
                "region": "us-east-1",
                "outputs": {"webhook_lambda": "dev-scheduler-webhook"},
            }),
        )
        yield s3


def test_list_registry_returns_all_stacks(registry_bucket):
    from shared.registry import list_registry, _clear_registry_cache
    _clear_registry_cache()

    stacks = list_registry()
    assert len(stacks) == 2
    projects = sorted(s["project"] for s in stacks)
    assert projects == sorted(["socialagent", "scheduler"])


def test_registry_listings_skip_the_access_log_prefix(registry_bucket, monkeypatch):
    """The per-admin registry bucket logs its own server access to
    s3-access-logs/, at the root beside the stacks. Neither listing takes it
    for a stack or logs a failed load for it."""
    from shared import registry as registry_mod
    from shared.registry import get_all_registries, list_registry, _clear_registry_cache
    _clear_registry_cache()

    registry_bucket.put_object(
        Bucket="test-registry",
        Key="s3-access-logs/EXAMPLE-LOG-OBJECT",
        Body=b"access log record",
    )
    failures = []
    monkeypatch.setattr(
        registry_mod.logger, "exception", lambda msg, **kw: failures.append(msg)
    )

    assert sorted(s["project"] for s in list_registry()) == ["scheduler", "socialagent"]
    assert sorted(get_all_registries()) == ["scheduler-dev", "socialagent-dev"]
    assert failures == []


def test_get_registry_returns_single_stack(registry_bucket):
    from shared.registry import get_registry, _clear_registry_cache
    _clear_registry_cache()

    stack = get_registry("socialagent-dev")
    assert stack is not None
    assert stack["project"] == "socialagent"


def test_get_registry_missing_stack_returns_none(registry_bucket):
    from shared.registry import get_registry, _clear_registry_cache
    _clear_registry_cache()

    assert get_registry("nonexistent") is None


def test_list_registry_caches_result(registry_bucket):
    from shared.registry import list_registry, _clear_registry_cache
    from shared import registry as registry_mod
    _clear_registry_cache()

    # Track S3 list_objects_v2 calls by wrapping the module's client factory
    call_count = {"n": 0}
    original_client = registry_mod.client

    def counting_client(service, region):
        c = original_client(service, region)
        if service == "s3":
            original_list = c.list_objects_v2

            def counting_list(**kwargs):
                call_count["n"] += 1
                return original_list(**kwargs)

            c.list_objects_v2 = counting_list
        return c

    registry_mod.client = counting_client
    try:
        with freeze_time("2026-05-08 12:00:00"):
            list_registry()
            list_registry()
            list_registry()
        # Three calls, but S3 list_objects_v2 should fire only once (cached)
        assert call_count["n"] == 1
    finally:
        registry_mod.client = original_client


def test_cache_expires_after_ttl(registry_bucket):
    from shared.registry import list_registry, _clear_registry_cache
    _clear_registry_cache()

    with freeze_time("2026-05-08 12:00:00"):
        list_registry()

    # REGISTRY_CACHE_TTL = 30s; at 31s cache should be stale
    with freeze_time("2026-05-08 12:00:31"):
        stacks = list_registry()
        assert len(stacks) == 2


def test_put_registry_writes_yaml_to_s3(registry_bucket):
    from shared.registry import put_registry, get_registry, _clear_registry_cache
    _clear_registry_cache()

    new_config = {
        "project": "newstack",
        "environment": "dev",
        "region": "us-east-1",
        "outputs": {},
    }
    assert put_registry("newstack-dev", new_config) is True

    # Verify round-trip by fetching via raw S3 (cache was cleared by put_registry)
    fetched = get_registry("newstack-dev")
    assert fetched == new_config


def test_read_all_registries_lists_the_entries_it_could_not_read(registry_bucket):
    """A change that must see every stack, such as a phone bind, cannot take a
    skipped entry for a stack that is not there."""
    from shared.registry import get_all_registries, read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    registry_bucket.put_object(Bucket="test-registry", Key="broken-dev/socialagent.yaml", Body=b"outputs: [unclosed")

    stacks, unread = read_all_registries()
    assert sorted(stacks) == ["scheduler-dev", "socialagent-dev"]
    assert unread == ["broken-dev"]
    # The cached listing still skips it, as the pages that use it expect.
    assert sorted(get_all_registries()) == ["scheduler-dev", "socialagent-dev"]
    _clear_registry_cache()


def test_read_all_registries_raises_when_the_listing_fails(registry_bucket, monkeypatch):
    from shared import registry as registry_mod
    from shared.registry import get_all_registries, read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    class Refusing:
        def list_objects_v2(self, **_kwargs):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(registry_mod, "client", lambda *_a: Refusing())

    with pytest.raises(RuntimeError):
        read_all_registries()
    assert get_all_registries() == {}
    _clear_registry_cache()


def test_read_all_registries_takes_a_prefix_with_no_config_for_no_stack(registry_bucket, monkeypatch):
    """A prefix that holds no socialagent.yaml, such as leftover tools.json or
    skill files, is no stack: get_registry answers None for it and
    list_registry skips it. Reported as unread, it blocked every phone bind
    and update-sns-destination in every Region."""
    from shared import registry as registry_mod
    from shared.registry import get_registry, list_registry, read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    registry_bucket.put_object(Bucket="test-registry", Key="gone-dev/tools.json", Body=b"[]")
    registry_bucket.put_object(Bucket="test-registry", Key="gone-dev/skills/notes.md", Body=b"# notes")
    failures = []
    monkeypatch.setattr(registry_mod.logger, "exception", lambda msg, **kw: failures.append(msg))

    stacks, unread = read_all_registries()

    assert sorted(stacks) == ["scheduler-dev", "socialagent-dev"]
    assert unread == []
    assert failures == []
    # The rest of the console agrees that it is no stack.
    assert get_registry("gone-dev") is None
    assert len(list_registry()) == 2
    _clear_registry_cache()


def test_read_all_registries_still_reports_an_entry_it_was_refused(registry_bucket, monkeypatch):
    """Only a missing socialagent.yaml means no stack. Any other failed read,
    such as an access denial, may hide a stack on the WABA, so it stays
    unread and the bind refuses."""
    from botocore.exceptions import ClientError

    from shared import registry as registry_mod
    from shared.registry import read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    s3 = registry_mod.client("s3", registry_mod.DEFAULT_REGION)
    real_get = s3.get_object

    def get_object(**kwargs):
        if kwargs["Key"].startswith("scheduler-dev/"):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetObject")
        return real_get(**kwargs)

    monkeypatch.setattr(s3, "get_object", get_object)

    stacks, unread = read_all_registries()

    assert sorted(stacks) == ["socialagent-dev"]
    assert unread == ["scheduler-dev"]
    _clear_registry_cache()


def test_read_all_registries_reads_every_page_of_the_listing(registry_bucket, monkeypatch):
    """ListObjectsV2 returns at most 1000 prefixes a call. The bind read only
    the first page, so past 1000 registered stacks it never saw the rest and
    could take a WABA they share for an unshared one. Pages of one prefix
    stand in for that here."""
    from shared import registry as registry_mod
    from shared.registry import read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    registry_bucket.put_object(Bucket="test-registry", Key="third-dev/socialagent.yaml", Body=yaml.dump({"project": "third"}))
    s3 = registry_mod.client("s3", registry_mod.DEFAULT_REGION)
    real_list = s3.list_objects_v2
    calls = []

    def list_one_at_a_time(**kwargs):
        calls.append(kwargs.get("ContinuationToken"))
        return real_list(**kwargs, MaxKeys=1)

    monkeypatch.setattr(s3, "list_objects_v2", list_one_at_a_time)

    stacks, unread = read_all_registries()

    assert sorted(stacks) == ["scheduler-dev", "socialagent-dev", "third-dev"]
    assert unread == []
    assert len(calls) == 3 and calls[0] is None and all(calls[1:])
    _clear_registry_cache()


def test_read_all_registries_raises_on_a_truncated_page_with_no_token(registry_bucket, monkeypatch):
    """A listing that says it is truncated but gives no token to go on cannot
    be read in full, so the read fails rather than answer the first page."""
    from shared import registry as registry_mod
    from shared.registry import read_all_registries, _clear_registry_cache
    _clear_registry_cache()

    s3 = registry_mod.client("s3", registry_mod.DEFAULT_REGION)
    real_list = s3.list_objects_v2

    def truncated(**kwargs):
        resp = real_list(**kwargs)
        resp["IsTruncated"] = True
        resp.pop("NextContinuationToken", None)
        return resp

    monkeypatch.setattr(s3, "list_objects_v2", truncated)

    with pytest.raises(RuntimeError, match="truncated"):
        read_all_registries()
    _clear_registry_cache()
