# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# SNS Topic for WhatsApp Messages via EUM Social

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "shared_sns_topic_arn" {
  type        = string
  default     = ""
  description = "If set, use this existing SNS topic instead of creating a new one. Used when multiple stacks share a WABA (one SNS per WABA) and filter at the Lambda level by Meta phone number ID."
}

variable "kms_key_arn" {
  type        = string
  default     = ""
  description = "ARN of a customer-managed KMS key for topic encryption. Empty (default) leaves the inbound WhatsApp topic unencrypted at rest. EUM Social does not require topic encryption (it is an optional hardening per the EUM Social docs); an AWS-managed key cannot be used here because its key policy is not editable to grant social-messaging.amazonaws.com. At-rest encryption via a customer-managed CMK is optional."
}

locals {
  prefix     = "${var.environment}-${var.project_name}"
  use_shared = var.shared_sns_topic_arn != ""
}

data "aws_caller_identity" "current" {}

# SNS Topic — receives inbound WhatsApp messages from EUM Social.
# Skipped entirely when reusing an existing topic from another stack.
resource "aws_sns_topic" "whatsapp" {
  count        = local.use_shared ? 0 : 1
  name         = "${local.prefix}-whatsapp"
  display_name = "${var.project_name} WhatsApp Messages"
  # Empty kms_key_arn => unencrypted at rest (CMK-based encryption is optional).
  # An AWS-managed key (alias/aws/sns) cannot be used: its policy can't grant EUM Social access.
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
}

# Allow EUM Social to publish to this topic
resource "aws_sns_topic_policy" "whatsapp" {
  count = local.use_shared ? 0 : 1
  arn   = aws_sns_topic.whatsapp[0].arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEUMSocialPublish"
      Effect    = "Allow"
      Principal = { Service = "social-messaging.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.whatsapp[0].arn
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })
}
