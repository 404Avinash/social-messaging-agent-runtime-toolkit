# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for shared.clients — cached boto3 client factory."""
from shared.clients import client, clear_client_cache


def setup_function():
    """Reset cache between tests to prevent cross-test pollution."""
    clear_client_cache()


def test_same_service_and_region_returns_cached_instance():
    c1 = client("s3", "us-east-1")
    c2 = client("s3", "us-east-1")
    assert c1 is c2


def test_different_regions_return_different_instances():
    c1 = client("s3", "us-east-1")
    c2 = client("s3", "us-west-2")
    assert c1 is not c2


def test_different_services_return_different_instances():
    c1 = client("s3", "us-east-1")
    c2 = client("dynamodb", "us-east-1")
    assert c1 is not c2


def test_client_has_adaptive_retry_config():
    c = client("s3", "us-east-1")
    retry_config = c.meta.config.retries
    # Botocore normalizes max_attempts=5 to total_max_attempts=6 (initial + 5 retries).
    # See botocore/config.py: Config._merge_retries.
    assert retry_config["mode"] == "adaptive"
    assert retry_config["total_max_attempts"] == 6


def test_clear_cache_forces_new_instance():
    c1 = client("s3", "us-east-1")
    clear_client_cache()
    c2 = client("s3", "us-east-1")
    assert c1 is not c2
