# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Memory
# =============================================================================
output "memory_id" {
  description = "AgentCore Memory ID"
  value       = module.memory.memory_id
}
output "memory_arn" {
  description = "AgentCore Memory ARN"
  value       = module.memory.memory_arn
}

# =============================================================================
# Runtime
# =============================================================================
output "runtime_id" {
  description = "AgentCore Runtime ID"
  value       = module.runtime.runtime_id
}
output "runtime_arn" {
  description = "AgentCore Runtime ARN"
  value       = module.runtime.runtime_arn
}
output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = module.runtime.ecr_repository_url
}
output "runtime_role_arn" {
  description = "Runtime execution role ARN"
  value       = module.iam.agentcore_runtime_role_arn
}

# =============================================================================
# SNS
# =============================================================================
output "sns_topic_arn" {
  description = "WhatsApp SNS topic ARN"
  value       = module.sns.topic_arn
}

# =============================================================================
# Lambdas
# =============================================================================
output "webhook_lambda_arn" {
  description = "Webhook Lambda ARN"
  value       = module.lambdas.webhook_arn
}
output "notification_lambda_arn" {
  description = "Notification Lambda ARN"
  value       = module.lambdas.notification_arn
}
output "event_lambda_arn" {
  description = "Event handler Lambda ARN"
  value       = module.lambdas.event_arn
}
output "lambda_function_names" {
  description = "All Lambda function names"
  value       = module.lambdas.all_function_names
}

# =============================================================================
# Event API
# =============================================================================
output "event_api_url" {
  description = "Event API Gateway URL"
  value       = module.event_api.api_url
}
output "event_api_key_id" {
  description = "Event API key ID (use: aws apigateway get-api-key --api-key ID --include-value)"
  value       = module.event_api.api_key_id
}

# =============================================================================
# Scheduler
# =============================================================================
output "scheduler_role_arn" {
  description = "EventBridge Scheduler role ARN"
  value       = module.iam.scheduler_role_arn
}

# =============================================================================
# Optional
# =============================================================================
output "state_table_name" {
  description = "DynamoDB state table name (if enabled)"
  value       = var.enable_state_table ? module.state[0].table_name : ""
}
output "guardrail_id" {
  description = "Bedrock guardrail the agent and the webhook use: the stack's, or the one terraform.guardrail names (its id or ARN). Empty when terraform.guardrail is none."
  value       = local.effective_guardrail_id

  # Checks guardrail_version against guardrail, which a variable validation
  # cannot do before Terraform 1.9 (variables.tf).
  precondition {
    condition     = var.guardrail_version == "" || local.guardrail_is_own
    error_message = "guardrail_version is only valid with a guardrail of your own, named by id or ARN in terraform.guardrail. The stack's guardrail is always called at the version the stack publishes (its draft equals that version right after an apply, since the policy lives in Terraform), and none has no version. Remove guardrail_version, or set guardrail to your guardrail's id or ARN."
  }

  # A guardrail is a Regional resource, and a Bedrock call can use only one in
  # its own Region, so an ARN in another Region or partition fails every agent
  # turn and every webhook description, whatever the grants say. Another
  # account in the stack's Region can work, so the check "guardrail_in_stack"
  # (main.tf) only warns about that one. The same locals as that check.
  precondition {
    condition     = !startswith(var.guardrail, "arn:") || startswith(var.guardrail, "arn:${local.partition}:bedrock:${local.region}:")
    error_message = "terraform.guardrail is a guardrail ARN in another partition or Region than the stack. A Bedrock call can use only a guardrail in its own Region, so every agent turn and every webhook description would fail. Use a guardrail in the stack's Region, by its id or by its ARN there."
  }
}

# =============================================================================
# Summary
# =============================================================================
output "deployment_summary" {
  description = "Summary of deployed resources"
  value = {
    project_name = var.project_name
    environment  = var.environment
    region       = local.region
    account_id   = local.account_id
    tracking_id  = local.tracking_id
    memory_id    = module.memory.memory_id
    runtime_arn  = module.runtime.runtime_arn
    event_api    = module.event_api.api_url
  }
}

output "event_api_webhook_secret_arn" {
  description = "Secrets Manager ARN for the event API webhook secret. External callers must send this value in the x-webhook-secret header."
  value       = aws_secretsmanager_secret.webhook_secret.arn
}
