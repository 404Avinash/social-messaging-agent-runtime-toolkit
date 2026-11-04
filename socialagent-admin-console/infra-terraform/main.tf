# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# SMART Admin Console — Frontend + Auth + API Backend

locals {
  prefix = "${var.environment}-${var.project_name}"
  common_tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
  })
}

# ── Frontend (S3 + CloudFront + OAC) ─────────────────────────────────

module "frontend" {
  source = "./modules/frontend"

  project_name = var.project_name
  environment  = var.environment
  build_dir    = var.build_dir
  tags         = local.common_tags
}

# ── Auth (Cognito User Pool + Identity Pool) ─────────────────────────

module "auth" {
  source = "./modules/auth"

  project_name      = var.project_name
  environment       = var.environment
  cloudfront_domain = module.frontend.cloudfront_domain
  admin_emails      = var.admin_emails
  tags              = local.common_tags
}

# ── Encryption ────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "main" {
  description             = "Admin console encryption key for ${var.project_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      # CloudWatch Logs encrypts the backend's log group with this key. Logs is a
      # service principal, not an IAM principal in this account, so the root
      # statement above does not reach it — without this the log group fails to
      # apply with "The specified KMS key does not exist or is not allowed to be
      # used". The encryption-context condition is the confused-deputy guard: it
      # limits use to log groups this console actually owns.
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.prefix}-*"
          }
        }
      },
    ]
  })
}

# ── API (API Gateway + Lambda + S3 Registry + DynamoDB Settings) ─────

module "api" {
  source = "./modules/api"

  project_name                = var.project_name
  environment                 = var.environment
  cognito_user_pool_arn       = module.auth.user_pool_arn
  cognito_user_pool_client_id = module.auth.client_id
  tags                        = local.common_tags
  kms_key_arn                 = aws_kms_key.main.arn

  # Scope CORS to the CloudFront console origin instead of "*". The
  # frontend module takes no API input, so this creates no dependency cycle.
  cors_allow_origin = module.frontend.cloudfront_url

  # Point registry at shared runtime bucket under admin/registry/ prefix.
  # Bucket is created by infra-terraform/bootstrap/ (deployed once per account).
  shared_runtime_bucket     = var.shared_runtime_bucket
  shared_runtime_bucket_arn = "arn:aws:s3:::${var.shared_runtime_bucket}"

  # Anti-lockout ordering: the backend Lambda enforces deny-by-default
  # authorization, so it must never deploy before the Cognito groups and
  # admin memberships exist. Referencing user_pool_arn alone only orders
  # against the user pool; this explicit dependency also waits for the
  # groups + admin_membership resources in the auth module.
  depends_on = [module.auth]
}
