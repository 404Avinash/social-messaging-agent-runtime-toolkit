# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans the stack root with mocked providers for each value of `guardrail`, as
# audit_trails_bucket_policy.tftest.hcl does for the bucket modes. The
# Terraform-backed tests in tests/test_terraform_hardening.py evaluate the
# choice in a root that holds only its own blocks; these runs plan the whole
# stack, so a reference to module.guardrails[0] outside the choice would fail
# the none and own runs, where the module has no instance.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
  mock_data "aws_region" {
    defaults = {
      id   = "us-east-1"
      name = "us-east-1"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
}
mock_provider "archive" {}
mock_provider "null" {}
mock_provider "random" {}

# A mocked plan leaves the module's outputs unknown, and a condition on an
# unknown value cannot be checked.
override_module {
  target          = module.guardrails
  override_during = plan
  outputs = {
    guardrail_id      = "stackgr00001"
    guardrail_version = "3"
    guardrail_arn     = "arn:aws:bedrock:us-east-1:123456789012:guardrail/stackgr00001"
  }
}

variables {
  project_name     = "guardrail-test"
  environment      = "dev"
  agent_source_dir = "../blueprints/minimal"
  handlers_dir     = "../blueprints/minimal/handlers"
}

run "stack_by_default" {
  command = plan

  assert {
    condition     = length(module.guardrails) == 1
    error_message = "By default the stack creates its own guardrail."
  }

  assert {
    condition     = [local.effective_guardrail_id, local.effective_guardrail_version, local.effective_guardrail_arn, output.guardrail_id] == ["stackgr00001", "3", "arn:aws:bedrock:us-east-1:123456789012:guardrail/stackgr00001", "stackgr00001"]
    error_message = "By default every call passes the stack's guardrail."
  }
}

run "none" {
  command = plan

  variables {
    guardrail = "none"
  }

  assert {
    condition     = length(module.guardrails) == 0
    error_message = "With none the stack creates no guardrail and no alarm."
  }

  assert {
    condition     = [local.effective_guardrail_id, local.effective_guardrail_version, local.effective_guardrail_arn, output.guardrail_id] == ["", "", "", ""]
    error_message = "With none no call passes a guardrail, and the webhook gets no grant."
  }
}

run "your_own_by_id" {
  command = plan

  variables {
    guardrail         = "abcd1234efgh"
    guardrail_version = "2"
  }

  assert {
    condition     = length(module.guardrails) == 0
    error_message = "With a guardrail of your own the stack creates none."
  }

  assert {
    condition     = [local.effective_guardrail_id, local.effective_guardrail_version, local.effective_guardrail_arn] == ["abcd1234efgh", "2", "arn:aws:bedrock:us-east-1:123456789012:guardrail/abcd1234efgh"]
    error_message = "A guardrail of your own is passed at its version, and its ARN is built in the stack's partition, Region and account."
  }
}

run "a_version_with_the_stacks_guardrail" {
  command = plan

  variables {
    guardrail_version = "2"
  }

  expect_failures = [output.guardrail_id]
}
