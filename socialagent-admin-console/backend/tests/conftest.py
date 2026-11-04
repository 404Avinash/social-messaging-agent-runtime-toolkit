# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures for backend tests."""
import pytest


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Set dummy AWS credentials + required env vars for every test.

    Prevents accidental real AWS calls; moto rejects real creds anyway.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("REGISTRY_BUCKET", "test-registry")
    monkeypatch.setenv("REGISTRY_PREFIX", "")
    monkeypatch.setenv("SETTINGS_TABLE", "test-settings")
