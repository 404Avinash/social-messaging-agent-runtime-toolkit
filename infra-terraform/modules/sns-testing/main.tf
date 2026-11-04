# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Dev/test notification topic

variable "project_name" { type = string }
variable "environment" { type = string }
variable "kms_key_arn" {
  type        = string
  default     = ""
  description = "ARN of the KMS key for topic encryption. Empty uses the AWS-managed key (alias/aws/sns); set a customer-managed CMK ARN to override."
}
variable "notification_email" {
  type    = string
  default = ""
}

resource "aws_sns_topic" "notifications" {
  name              = "${var.environment}-${var.project_name}-test-notifications"
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

output "topic_arn" { value = aws_sns_topic.notifications.arn }
output "topic_name" { value = aws_sns_topic.notifications.name }
