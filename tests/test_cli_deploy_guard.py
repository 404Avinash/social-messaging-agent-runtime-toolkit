# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``socialagent deploy`` refuses to apply a plan that destroys or replaces a
resource holding the stack's data unless the operator says so explicitly. A
change of bucket layout, state prefix or workspace name plans exactly such a
destroy, and the data does not move."""

import json

import pytest

from socialagent.cli._deploy import STATEFUL_TYPES, data_loss_in_plan
from socialagent.cli._errors import CliError


def _plan(*changes: tuple[str, str, list[str]]) -> str:
    """A ``terraform show -json`` document with the given (address, type, actions)."""
    return json.dumps(
        {
            "format_version": "1.2",
            "resource_changes": [
                {"address": addr, "type": typ, "change": {"actions": actions}}
                for addr, typ, actions in changes
            ],
        }
    )


def test_stateful_types_cover_every_store_the_stack_creates():
    assert STATEFUL_TYPES >= {
        "aws_s3_bucket",
        "aws_dynamodb_table",
        "aws_bedrockagentcore_memory",
        "aws_secretsmanager_secret",
    }


def test_create_and_update_are_not_data_loss():
    plan = _plan(
        ("aws_s3_bucket.logs[0]", "aws_s3_bucket", ["create"]),
        ("aws_dynamodb_table.state[0]", "aws_dynamodb_table", ["update"]),
        ("aws_lambda_function.webhook", "aws_lambda_function", ["delete", "create"]),
    )
    assert data_loss_in_plan(plan) == []


def test_delete_of_a_bucket_is_data_loss():
    plan = _plan(("module.x.aws_s3_bucket.logs[0]", "aws_s3_bucket", ["delete"]))
    assert data_loss_in_plan(plan) == [
        "module.x.aws_s3_bucket.logs[0] (aws_s3_bucket): delete"
    ]


def test_replace_of_memory_is_data_loss_in_either_order():
    plan = _plan(
        (
            "module.memory.aws_bedrockagentcore_memory.main",
            "aws_bedrockagentcore_memory",
            ["delete", "create"],
        ),
        ("aws_dynamodb_table.state[0]", "aws_dynamodb_table", ["create", "delete"]),
    )
    found = data_loss_in_plan(plan)
    assert len(found) == 2 and all("replace" in f for f in found)


def test_no_op_and_read_are_ignored():
    plan = _plan(
        ("aws_s3_bucket.logs[0]", "aws_s3_bucket", ["no-op"]),
        ("data.aws_s3_bucket.shared", "aws_s3_bucket", ["read"]),
    )
    assert data_loss_in_plan(plan) == []


def test_malformed_plan_json_is_a_clear_error():
    with pytest.raises(CliError, match="plan"):
        data_loss_in_plan("not json")


def test_plan_without_resource_changes_is_empty():
    assert data_loss_in_plan(json.dumps({"format_version": "1.2"})) == []
