# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Environment each stack Lambda gets from infra-terraform/modules/lambdas.

``EUMSocialProvider.upload_media_from_s3`` refuses any upload when
``CONVERSATION_LOGS_BUCKET`` is unset, and the notification Lambda is the one
that performs uploads for agent tools (``notify.upload_media``: voice replies,
generated images). Without the variable every such upload fails with "no media
bucket configured".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LAMBDAS_TF = (
    Path(__file__).parent.parent / "infra-terraform" / "modules" / "lambdas" / "main.tf"
)


def _env_block(function: str) -> str:
    """The text of one aws_lambda_function's environment { variables = ... }."""
    tf = LAMBDAS_TF.read_text()
    start = tf.index(f'resource "aws_lambda_function" "{function}"')
    body = tf[start : tf.index("\n}\n", start)]
    return body[body.index("environment {") :]


def _common_env() -> str:
    tf = LAMBDAS_TF.read_text()
    start = tf.index("common_env = merge(")
    return tf[start : tf.index("\n  )\n", start)]


def _has(var: str, function: str) -> bool:
    pattern = re.compile(rf"^\s*{var}\s*=", re.M)
    block = _env_block(function)
    via_common = "local.common_env" in block and pattern.search(_common_env())
    return bool(pattern.search(block) or via_common)


@pytest.mark.parametrize("function", ["notification", "webhook", "event", "scheduler"])
def test_every_lambda_knows_the_media_bucket(function: str) -> None:
    assert _has("CONVERSATION_LOGS_BUCKET", function), (
        f"{function} Lambda has no CONVERSATION_LOGS_BUCKET, so media uploads "
        "from it are refused"
    )


def test_notification_role_can_upload_media() -> None:
    """The notification Lambda runs under its own role; the upload it performs
    needs PostWhatsAppMessageMedia and read access to the media object."""
    tf = (LAMBDAS_TF.parent.parent / "iam" / "tool-roles.tf").read_text()
    start = tf.index('resource "aws_iam_role_policy" "notification_service"')
    policy = tf[start : tf.index("\n}\n", start)]
    assert "social-messaging:PostWhatsAppMessageMedia" in policy
    assert '"s3:GetObject"' in policy


def test_notification_media_read_excludes_audit_trails() -> None:
    """The media read grant must not ride on s3_bucket_arns: that list also holds
    the audit-trails bucket (CloudTrail and Bedrock invocation logs), which the
    notification Lambda has no reason to read. It is scoped to the media bucket's
    three upload roots instead."""
    tf = (LAMBDAS_TF.parent.parent / "iam" / "tool-roles.tf").read_text()
    start = tf.index('Sid    = "MediaUploadSourceRead"')
    statement = tf[tf.rindex("? [{", 0, start) : tf.index("}] : []", start)]
    assert "var.s3_bucket_arns" not in statement
    assert "var.media_bucket_arn" in statement
    assert '["voice", "images", "media"]' in statement
