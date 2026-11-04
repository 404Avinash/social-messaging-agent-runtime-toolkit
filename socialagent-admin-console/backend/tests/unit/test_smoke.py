# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Smoke test — verifies test infrastructure works end-to-end."""
import os


def test_aws_env_fixture_sets_credentials():
    assert os.environ["AWS_ACCESS_KEY_ID"] == "testing"
    assert os.environ["AWS_DEFAULT_REGION"] == "us-east-1"
    assert os.environ["REGISTRY_BUCKET"] == "test-registry"
