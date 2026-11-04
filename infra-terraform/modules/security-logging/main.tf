# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# CloudTrail data events + Bedrock invocation logging

variable "project_name" { type = string }
variable "environment" { type = string }
variable "audit_trails_bucket_name" { type = string }
variable "audit_trails_bucket_arn" { type = string }
variable "audit_trails_key_prefix" {
  type        = string
  default     = ""
  description = "Optional key prefix within audit_trails_bucket for this stack (e.g. 'stacks/socialagent-dev/audit-trails'). When empty, writes to bucket root."
}
variable "cloudtrail_log_retention_days" {
  type    = number
  default = 60
}

locals {
  prefix                = "${var.environment}-${var.project_name}"
  cloudtrail_key_prefix = var.audit_trails_key_prefix != "" ? "${var.audit_trails_key_prefix}/cloudtrail" : "cloudtrail"
  bedrock_key_prefix    = var.audit_trails_key_prefix != "" ? "${var.audit_trails_key_prefix}/bedrock-logs" : "bedrock-logs"
}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# The grants CloudTrail and Bedrock need on the audit-trails bucket. This module
# owns no bucket policy: S3 keeps one policy per bucket, so the bucket's owner
# puts these statements in it. That is the root module for a per-stack bucket
# (aws_s3_bucket_policy.audit_trails, with the bucket's other statements) and
# bootstrap for the shared runtime bucket, which writes its own.
output "bucket_policy_statements" {
  description = "Bucket policy statements that let CloudTrail and Bedrock write their logs to the audit-trails bucket."
  value = [
    {
      Sid       = "CloudTrailAclCheck"
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "s3:GetBucketAcl"
      Resource  = var.audit_trails_bucket_arn
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    },
    {
      Sid       = "CloudTrailWrite"
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${var.audit_trails_bucket_arn}/${local.cloudtrail_key_prefix}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control", "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    },
    {
      Sid       = "BedrockLoggingWrite"
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${var.audit_trails_bucket_arn}/${local.bedrock_key_prefix}/*"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    },
  ]
}

# This module used to write the per-stack bucket's policy as well, so two
# resources rewrote one policy on every apply. Forget that resource without
# destroying it: a destroy would call DeleteBucketPolicy on the live bucket,
# and the root module's policy now carries these statements.
removed {
  from = aws_s3_bucket_policy.audit_trails
  lifecycle {
    destroy = false
  }
}

# CloudWatch log group for CloudTrail
resource "aws_cloudwatch_log_group" "cloudtrail" {
  name              = "/aws/cloudtrail/${local.prefix}"
  retention_in_days = var.cloudtrail_log_retention_days
}

# IAM: CloudTrail → CloudWatch Logs
resource "aws_iam_role" "cloudtrail_cw" {
  name = "${local.prefix}-cloudtrail-cw"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "cloudtrail.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "cloudtrail_cw" {
  name = "${local.prefix}-cloudtrail-cw"
  role = aws_iam_role.cloudtrail_cw.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.cloudtrail.arn}:*" }]
  })
}

# CloudTrail — Bedrock model invocation data events
resource "aws_cloudtrail" "data_events" {
  name                       = "${local.prefix}-data-events"
  s3_bucket_name             = var.audit_trails_bucket_name
  s3_key_prefix              = local.cloudtrail_key_prefix
  is_multi_region_trail      = false
  enable_log_file_validation = true
  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.cloudtrail_cw.arn

  advanced_event_selector {
    name = "Bedrock Model Invocations"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::Bedrock::Model"]
    }
  }
}

# Bedrock model invocation logging — metadata only (model id, caller identity,
# token counts, latency). Every data-delivery flag is pinned to false because
# the provider defaults an unset one to true, and text delivery copies every
# prompt and completion — WhatsApp message text, phone numbers, memory context —
# into the audit-trails bucket in plaintext. The provider exposes no audio flag;
# this stack invokes no Bedrock audio model (voice replies use Polly).
#
# This resource is an account-and-region singleton: applying it sets the logging
# configuration for every Bedrock caller in the account, not just this stack.
resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  logging_config {
    text_data_delivery_enabled      = false
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false
    video_data_delivery_enabled     = false
    s3_config {
      bucket_name = var.audit_trails_bucket_name
      key_prefix  = local.bedrock_key_prefix
    }
  }
}
