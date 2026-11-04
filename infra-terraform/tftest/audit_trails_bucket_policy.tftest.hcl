# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans the stack root with mocked providers: no credentials, no state, nothing
# created. `make tf-validate` runs it with
# `terraform test -test-directory=tftest`. It lives outside the default tests/
# directory because `override_during` needs Terraform 1.11, and `terraform init`
# and `validate` parse tests/ on every run, which would raise the deploy
# minimum (versions.tf) for a test.
#
# S3 keeps one policy per bucket. These runs pin that the audit-trails bucket
# gets exactly one aws_s3_bucket_policy in each mode, and that it carries the
# security-logging module's CloudTrail and Bedrock grants and the grant that
# lets S3 deliver the media bucket's server access logs to it. On a shared
# runtime bucket bootstrap writes the policy; the last run pins where the stack's
# logs go there, and tests/test_terraform_hardening.py checks that bootstrap's
# grants cover it.

# 123456789012 is the AWS documentation's placeholder account. Every
# aws_caller_identity, the root's and each module's, reads the same one, so the
# grants' account conditions and AWSLogs/ prefix can be checked.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
}
mock_provider "archive" {}
mock_provider "null" {}
mock_provider "random" {}

# The policy is built from the buckets' ARNs, which a mocked plan leaves
# unknown unless they are overridden.
override_resource {
  target          = aws_s3_bucket.audit_trails
  override_during = plan
  values = {
    id  = "policy-test-audit-trails"
    arn = "arn:aws:s3:::policy-test-audit-trails"
  }
}

override_resource {
  target          = aws_s3_bucket.conversation_logs
  override_during = plan
  values = {
    id  = "policy-test-conversation-logs"
    arn = "arn:aws:s3:::policy-test-conversation-logs"
  }
}

variables {
  project_name     = "policy-test"
  environment      = "dev"
  agent_source_dir = "../blueprints/minimal"
  handlers_dir     = "../blueprints/minimal/handlers"
}

run "per_stack_with_security_logging" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket_policy.audit_trails) == 1
    error_message = "A per-stack audit-trails bucket needs its bucket policy."
  }

  assert {
    condition     = aws_s3_bucket_policy.audit_trails[0].bucket == "policy-test-audit-trails" && aws_s3_bucket_policy.conversation_logs[0].bucket == "policy-test-conversation-logs"
    error_message = "Each per-stack bucket policy must sit on its own bucket."
  }

  assert {
    condition = [
      for s in jsondecode(aws_s3_bucket_policy.audit_trails[0].policy).Statement : s.Sid
      ] == [
      "DenyInsecureTransport",
      "S3ServerAccessLogsConversationLogs",
      "CloudTrailAclCheck",
      "CloudTrailWrite",
      "BedrockLoggingWrite",
    ]
    error_message = "The audit-trails policy must hold the TLS deny, the access-log grant and the CloudTrail and Bedrock grants, each once."
  }

  assert {
    condition     = jsonencode(slice(jsondecode(aws_s3_bucket_policy.audit_trails[0].policy).Statement, 2, 5)) == jsonencode(module.security_logging[0].bucket_policy_statements)
    error_message = "The audit-trails policy must carry the security-logging module's statements unchanged."
  }

  assert {
    condition = {
      for s in module.security_logging[0].bucket_policy_statements : s.Sid => s.Resource
      } == {
      CloudTrailAclCheck  = "arn:aws:s3:::policy-test-audit-trails"
      CloudTrailWrite     = "arn:aws:s3:::policy-test-audit-trails/cloudtrail/AWSLogs/123456789012/*"
      BedrockLoggingWrite = "arn:aws:s3:::policy-test-audit-trails/bedrock-logs/*"
    }
    error_message = "The CloudTrail and Bedrock grants must cover the prefixes the trail and the logging configuration write to."
  }

  assert {
    condition     = aws_s3_bucket_logging.conversation_logs[0].target_bucket == "policy-test-audit-trails"
    error_message = "The media bucket must log to the audit-trails bucket."
  }

  assert {
    condition = jsonencode({
      for s in jsondecode(aws_s3_bucket_policy.audit_trails[0].policy).Statement : s.Sid => s
      if startswith(s.Sid, "S3ServerAccessLogs")
      }) == jsonencode({
      S3ServerAccessLogsConversationLogs = {
        Sid       = "S3ServerAccessLogsConversationLogs"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "arn:aws:s3:::policy-test-audit-trails/${aws_s3_bucket_logging.conversation_logs[0].target_prefix}*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = "123456789012" }
          ArnEquals    = { "aws:SourceArn" = "arn:aws:s3:::policy-test-conversation-logs" }
        }
      }
    })
    error_message = "The logging service may write the media bucket's logs under its target_prefix only, for this account's bucket, and nothing else."
  }

  assert {
    condition     = aws_s3_bucket_logging.conversation_logs[0].target_prefix == "s3-access-logs/conversation-logs/"
    error_message = "The media bucket logs under a prefix of its own."
  }
}

run "per_stack_without_security_logging" {
  command = plan

  variables {
    enable_security_logging = false
  }

  assert {
    condition     = length(aws_s3_bucket_policy.audit_trails) == 1
    error_message = "A per-stack audit-trails bucket needs its bucket policy without security logging too."
  }

  assert {
    condition = [
      for s in jsondecode(aws_s3_bucket_policy.audit_trails[0].policy).Statement : s.Sid
      ] == [
      "DenyInsecureTransport",
      "S3ServerAccessLogsConversationLogs",
    ]
    error_message = "Without security logging the audit-trails policy grants CloudTrail and Bedrock nothing, and still lets S3 deliver the media bucket's access logs."
  }
}

run "shared_runtime_bucket" {
  command = plan

  variables {
    shared_runtime_bucket = "policy-test-shared-runtime"
  }

  assert {
    condition     = length(aws_s3_bucket_policy.audit_trails) == 0 && length(aws_s3_bucket_policy.conversation_logs) == 0
    error_message = "With a shared runtime bucket the stack writes no bucket policy: bootstrap owns the shared bucket's."
  }

  assert {
    condition = {
      for s in module.security_logging[0].bucket_policy_statements : s.Sid => s.Resource
      } == {
      CloudTrailAclCheck  = "arn:aws:s3:::policy-test-shared-runtime"
      CloudTrailWrite     = "arn:aws:s3:::policy-test-shared-runtime/stacks/policy-test-dev/audit-trails/cloudtrail/AWSLogs/123456789012/*"
      BedrockLoggingWrite = "arn:aws:s3:::policy-test-shared-runtime/stacks/policy-test-dev/audit-trails/bedrock-logs/*"
    }
    error_message = "On a shared runtime bucket the trail and Bedrock logging write under the stack's stacks/<stack id>/audit-trails/ prefix, the one bootstrap's policy grants."
  }
}
