# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration test for GET /api/stacks.

Exercises the handler Lambda entry point with a fully mocked AWS
environment (S3 registry, DynamoDB settings, Lambda stub). Validates
the response structure matches what the admin console frontend expects.
"""
import json
from unittest.mock import MagicMock

import boto3
import pytest
import yaml
from moto import mock_aws


def _lambda_context() -> MagicMock:
    """Mock Lambda context with fields Powertools Logger reads."""
    ctx = MagicMock()
    ctx.function_name = "test-admin-api"
    ctx.function_version = "$LATEST"
    ctx.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test-admin-api"
    ctx.memory_limit_in_mb = 512
    ctx.aws_request_id = "test-req-id"
    return ctx


@pytest.fixture
def aws_setup():
    with mock_aws():
        # Registry bucket + 2 sample stacks
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-registry")
        for stack_id, project in [("socialagent-dev", "socialagent"), ("scheduler-dev", "scheduler")]:
            s3.put_object(
                Bucket="test-registry",
                Key=f"{stack_id}/socialagent.yaml",
                Body=yaml.dump({
                    "project": project,
                    "environment": "dev",
                    "region": "us-east-1",
                    "outputs": {
                        "webhook_lambda": f"dev-{project}-webhook",
                        "runtime_id": f"dev_{project}_runtime-abc",
                        "memory_id": f"dev_{project}_memory-xyz",
                    },
                }),
            )

        # Settings table
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="test-settings",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Register stub Lambdas for each stack's probe lambdas.
        # Moto 5.x validates the IAM role trust policy, so we create a real role first.
        iam = boto3.client("iam", region_name="us-east-1")
        trust = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        })
        role = iam.create_role(RoleName="test-lambda-role", AssumeRolePolicyDocument=trust)
        role_arn = role["Role"]["Arn"]

        lam = boto3.client("lambda", region_name="us-east-1")
        for project in ["socialagent", "scheduler"]:
            for suffix in ["webhook", "notification", "event", "scheduler"]:
                lam.create_function(
                    FunctionName=f"dev-{project}-{suffix}",
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="index.handler",
                    Code={"ZipFile": b"def handler(e,c): return {'statusCode': 200}"},
                )

        yield


def _api_gw_event(method: str, path: str, groups: str = "admin") -> dict:
    # `groups` populates the cognito:groups JWT claim that require_group (the
    # deny-by-default authZ check) reads. Default "admin" satisfies
    # every route level; pass a lower/empty value to exercise denial. Live
    # Cognito lookup is off in tests (COGNITO_USER_POOL_ID unset), so the signed
    # claim is authoritative here.
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "test-api",
            "domainName": "test.execute-api.us-east-1.amazonaws.com",
            "domainPrefix": "test",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test",
            },
            "authorizer": {"jwt": {"claims": {"sub": "test-user", "email": "admin@test", "cognito:groups": groups}}},
            "requestId": "test-req-id",
            "routeKey": f"{method} {path}",
            "stage": "$default",
            "time": "08/May/2026:12:00:00 +0000",
            "timeEpoch": 1746700800000,
        },
        "isBase64Encoded": False,
    }


def test_list_stacks_returns_200_with_registered_stacks(aws_setup):
    from handler import handler

    resp = handler(_api_gw_event("GET", "/api/stacks"), _lambda_context())

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert isinstance(body, list)
    assert len(body) == 2
    projects = sorted(s["stack"]["project"] for s in body)
    assert projects == sorted(["socialagent", "scheduler"])
    # Structure sanity checks — fields the frontend depends on
    for entry in body:
        assert "health" in entry
        assert "runtimeStatus" in entry
        assert "lambdas" in entry
        assert isinstance(entry["lambdas"], list)


def test_list_stacks_tolerates_cloudwatch_failure(aws_setup, monkeypatch):
    """If CloudWatch throws, response should still be 200 with graceful degradation."""
    from handler import handler
    import routes.stacks as stacks_mod
    from botocore.exceptions import ClientError

    def failing_metric(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "GetMetricData",
        )

    # The cloudwatch client is the `cw` singleton in shared.aws; routes.stacks
    # imports it by name, so patch it on the module that actually calls it.
    monkeypatch.setattr(stacks_mod.cw, "get_metric_data", failing_metric)

    resp = handler(_api_gw_event("GET", "/api/stacks"), _lambda_context())
    assert resp["statusCode"] == 200  # graceful degradation, not 5xx


def test_list_stacks_denies_caller_without_group(aws_setup):
    """Deny-by-default: a caller with no cognito:groups claim is
    rejected with 403, not served the stack list."""
    from handler import handler

    resp = handler(_api_gw_event("GET", "/api/stacks", groups=""), _lambda_context())

    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["requiredGroup"] == "viewer"
