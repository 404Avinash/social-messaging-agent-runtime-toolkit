# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Environment-driven configuration for the admin backend.

Required env vars crash fast on module import with a clear operator-facing
message — preferable to failing on the first request. If you see the error,
set the missing variable in the Lambda's environment block (see
infra-terraform/modules/api/main.tf aws_lambda_function.backend).
"""
from __future__ import annotations

import os
import sys

try:
    REGISTRY_BUCKET: str = os.environ["REGISTRY_BUCKET"]
    SETTINGS_TABLE: str = os.environ["SETTINGS_TABLE"]
except KeyError as e:
    print(
        f"ERROR: missing required env var: {e}. "
        "Configure it on the Lambda function (see aws_lambda_function.backend "
        "environment block) and redeploy.",
        file=sys.stderr,
    )
    raise

REGISTRY_PREFIX: str = os.environ.get("REGISTRY_PREFIX", "").strip("/")
DEFAULT_REGION: str = os.environ.get("AWS_REGION", "us-east-1")

# Comma-separated list of origins allowed by the admin API CORS headers.
# Empty falls back to "*" at the response layer (safe default for dev; set to
# the console's CloudFront/custom-domain origin in prod).
CORS_ALLOW_ORIGIN: str = os.environ.get("CORS_ALLOW_ORIGIN", "")

# Cognito User Pool id, used by the admin user-management routes.
COGNITO_USER_POOL_ID: str = os.environ.get("COGNITO_USER_POOL_ID", "")

# Cache TTL for registry reads (seconds). Lower = fresher, higher = fewer S3 calls.
# 30s strikes a balance: admin changes visible within 30s, ~90% S3 read reduction
# on warm Overview page loads.
REGISTRY_CACHE_TTL = 30
