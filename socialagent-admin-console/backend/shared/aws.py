# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared boto3 clients for the admin console backend.

Centralized so route modules can import the same client instances the
monolithic handler.py used, without each re-creating them. Created once per
Lambda container at import time.
"""
from __future__ import annotations

import boto3

from shared.config import DEFAULT_REGION, SETTINGS_TABLE

# Alias kept for the many `from shared.aws import REGION` call sites; the value
# is owned by shared.config (single source for env-driven configuration).
REGION = DEFAULT_REGION

s3 = boto3.client("s3", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
codebuild = boto3.client("codebuild", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

settings_table = dynamodb.Table(SETTINGS_TABLE)
