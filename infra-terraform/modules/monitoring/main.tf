# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Lambda-level monitoring: error alarms, duration alarms, dashboard

variable "project_name" { type = string }
variable "environment" { type = string }
variable "lambda_function_names" { type = list(string) }
variable "webhook_function_name" {
  description = "Webhook Lambda name for P95 duration alarm."
  type        = string
  default     = ""
}
variable "kms_key_arn" {
  description = "ARN of a customer-managed KMS key for the alarm topic. Empty (default) leaves the topic unencrypted at rest. The AWS-managed key (alias/aws/sns) cannot be used: its key policy does not let CloudWatch use it and cannot be changed, so no alarm could notify. A CMK's key policy must allow cloudwatch.amazonaws.com kms:Decrypt and kms:GenerateDataKey*."
  type        = string
  default     = ""
}
variable "alarm_email" {
  type    = string
  default = ""
}
variable "enable_pii_protection" {
  description = "Enable CloudWatch Logs data protection to mask PII (phone numbers, names, emails)."
  type        = bool
  default     = true
}

# CloudWatch Logs PhoneNumber is a region-dependent managed data identifier: it
# REQUIRES a two-letter country suffix and only certain countries are supported
# (BR, DE, ES, FR, GB, IT, US). A bare "PhoneNumber" is rejected by the API.
# +1 numbers (US and Canada share country code +1) are matched by PhoneNumber-US;
# CloudWatch Logs does not provide PhoneNumber-CA or PhoneNumber-MX.
variable "pii_phone_number_countries" {
  description = "Two-letter country codes for the CloudWatch Logs PhoneNumber data identifier. Supported: BR, DE, ES, FR, GB, IT, US. (+1 US/Canada -> US; Mexico is not supported by CWL.)"
  type        = list(string)
  default     = ["US", "BR"]

  validation {
    condition     = length(setsubtract(toset(var.pii_phone_number_countries), toset(["BR", "DE", "ES", "FR", "GB", "IT", "US"]))) == 0
    error_message = "pii_phone_number_countries may only contain CloudWatch Logs-supported codes: BR, DE, ES, FR, GB, IT, US (no CA/MX)."
  }
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # PhoneNumber-<CC> entries plus the region-independent Name / EmailAddress.
  pii_data_identifiers = concat(
    [for cc in var.pii_phone_number_countries : "arn:aws:dataprotection::aws:data-identifier/PhoneNumber-${cc}"],
    [
      "arn:aws:dataprotection::aws:data-identifier/Name",
      "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
    ],
  )
}
data "aws_region" "current" {}

# Log groups
resource "aws_cloudwatch_log_group" "lambda_logs" {
  for_each          = toset(var.lambda_function_names)
  name              = "/aws/lambda/${each.value}"
  retention_in_days = 30
}

# Alarm SNS topic. CloudWatch alarm actions publish here, so it carries alarm
# metadata only: alarm name, state, reason, metric and dimensions.
resource "aws_sns_topic" "alarms" {
  name = "${local.prefix}-alarms"
  # Empty kms_key_arn => unencrypted at rest, like the inbound WhatsApp topic.
  # An AWS-managed key (alias/aws/sns) cannot be used: its policy can't grant
  # CloudWatch access, so every alarm action would fail.
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Per-Lambda error alarms
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = toset(var.lambda_function_names)
  alarm_name          = "${local.prefix}-errors-${each.value}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Lambda error rate exceeded threshold"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions          = { FunctionName = each.value }
}

# Webhook P95 duration alarm
resource "aws_cloudwatch_metric_alarm" "webhook_duration" {
  count               = var.webhook_function_name != "" ? 1 : 0
  alarm_name          = "${local.prefix}-webhook-p95-duration"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  extended_statistic  = "p95"
  threshold           = 3000
  alarm_description   = "Webhook Lambda P95 duration exceeded 3s"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions          = { FunctionName = var.webhook_function_name }
}

# Dashboard
resource "aws_cloudwatch_dashboard" "lambdas" {
  dashboard_name = "${local.prefix}-lambdas"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        properties = {
          metrics = [for fn in var.lambda_function_names : ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum" }]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
          title   = "Lambda Invocations"
        }
      },
      {
        type = "metric"
        properties = {
          metrics = [for fn in var.lambda_function_names : ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum" }]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
          title   = "Lambda Errors"
        }
      },
    ]
  })
}

# =============================================================================
# PII Data Protection — masks phone numbers, names, emails in log streams
# =============================================================================

resource "aws_cloudwatch_log_data_protection_policy" "pii" {
  for_each       = var.enable_pii_protection ? toset(var.lambda_function_names) : toset([])
  log_group_name = "/aws/lambda/${each.value}"

  policy_document = jsonencode({
    Name    = "pii-protection"
    Version = "2021-06-01"

    Statement = [
      {
        Sid            = "audit-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation = {
          Audit = {
            FindingsDestination = {}
          }
        }
      },
      {
        Sid            = "mask-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation = {
          Deidentify = {
            MaskConfig = {}
          }
        }
      },
    ]
  })

  depends_on = [aws_cloudwatch_log_group.lambda_logs]
}

output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
output "dashboard_name" { value = aws_cloudwatch_dashboard.lambdas.dashboard_name }
