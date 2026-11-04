# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# AgentCore Memory — Conversation State Management
# Memory resource plus its execution role

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "event_expiry_days" {
  description = "Number of days to retain conversation events."
  type        = number
  default     = 7
}

locals {
  # Memory names only allow alphanumeric and underscores, must start with letter
  prefix = "${var.environment}_${replace(var.project_name, "-", "_")}"
}

# IAM Role for Memory Execution (required for long-term memory strategies)
resource "aws_iam_role" "memory_execution" {
  name = "${var.environment}-${var.project_name}-memory-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "memory_bedrock" {
  role       = aws_iam_role.memory_execution.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

# AgentCore Memory Resource
resource "aws_bedrockagentcore_memory" "main" {
  tags                      = var.tags
  name                      = "${local.prefix}_memory"
  description               = "Conversation memory for ${var.project_name}"
  event_expiry_duration     = var.event_expiry_days
  memory_execution_role_arn = aws_iam_role.memory_execution.arn
}

variable "tags" {
  type    = map(string)
  default = {}
}
