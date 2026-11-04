# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "topic_arn" {
  description = "ARN of the WhatsApp SNS topic (created or shared)"
  value       = local.use_shared ? var.shared_sns_topic_arn : aws_sns_topic.whatsapp[0].arn
}

output "topic_name" {
  description = "Name of the WhatsApp SNS topic"
  value       = local.use_shared ? split(":", var.shared_sns_topic_arn)[5] : aws_sns_topic.whatsapp[0].name
}
