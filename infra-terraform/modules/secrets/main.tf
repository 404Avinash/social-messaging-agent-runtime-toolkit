# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Generic Secrets Manager entries

variable "project_name" { type = string }
variable "environment" { type = string }

variable "secrets" {
  description = "Map of secret name → key-value pairs."
  type        = map(map(string))
  default     = {}
  sensitive   = true
}

variable "secret_names" {
  description = "List of secret names to create (keys of the secrets map). Needed because sensitive values can't be used in for_each."
  type        = list(string)
  default     = []
}

variable "runtime_role_id" {
  description = "AgentCore Runtime IAM role name (for read access)."
  type        = string
  default     = ""
}

variable "lambda_role_id" {
  description = "Lambda execution IAM role name (for read access)."
  type        = string
  default     = ""
}

variable "handler_role_name" {
  description = "IAM role name of the event and scheduler Lambdas, where @app.on_event / @app.on_schedule project code runs (for read access)."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "ARN of KMS key for encrypting secrets. Uses AWS-managed key if empty."
  type        = string
  default     = ""
}

locals { prefix = "${var.environment}-${var.project_name}" }

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(var.secret_names)

  name                    = "${local.prefix}-${each.key}"
  description             = "${each.key} credentials for ${var.project_name}"
  recovery_window_in_days = 7
  kms_key_id              = var.kms_key_arn != "" ? var.kms_key_arn : null
}

# Seeds the secret once and then leaves it alone. Values are set out of band
# with `socialagent secrets set <name>` (the CLI keeps secret names in config, never values), so they never
# have to live in a project file, a generated tfvars, or — after creation —
# be re-written from Terraform on every apply. A project listing names only
# seeds `{}`; a hand-written tfvars that still carries values seeds those.
# Either way a later change to var.secrets does NOT update the value: use
# `socialagent secrets set` (or the console / aws secretsmanager).
resource "aws_secretsmanager_secret_version" "this" {
  for_each = toset(var.secret_names)

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = jsonencode(var.secrets[each.key])

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# Grant read access to runtime role
resource "aws_iam_role_policy" "runtime_secrets" {
  count = var.runtime_role_id != "" && length(var.secrets) > 0 ? 1 : 0

  name = "${local.prefix}-runtime-secrets"
  role = var.runtime_role_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.this : s.arn]
    }]
  })
}

# Grant read access to Lambda role
resource "aws_iam_role_policy" "lambda_secrets" {
  count = var.lambda_role_id != "" && length(var.secrets) > 0 ? 1 : 0

  name = "${local.prefix}-lambda-secrets"
  role = var.lambda_role_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.this : s.arn]
    }]
  })
}

# Grant read access to the event/scheduler handler role. The notification
# Lambda is left out on purpose: it runs only SDK code (the blueprints'
# handlers/notification.py is a thin wrapper), so it has no reason to read
# project credentials.
resource "aws_iam_role_policy" "handler_secrets" {
  count = var.handler_role_name != "" && length(var.secret_names) > 0 ? 1 : 0

  name = "${local.prefix}-handler-secrets"
  role = var.handler_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.this : s.arn]
    }]
  })
}

output "secret_arns" {
  value = [for s in aws_secretsmanager_secret.this : s.arn]
}

output "secret_names" {
  value = { for k, s in aws_secretsmanager_secret.this : k => s.name }
}
