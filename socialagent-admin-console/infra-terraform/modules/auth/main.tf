# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Cognito User Pool + Identity Pool + IAM Role
# Auth is handled client-side by the React app (Cognito hosted UI / Amplify).
# Authenticated users get temporary IAM credentials via Identity Pool.
# No Lambda@Edge — CloudFront serves the SPA shell to everyone,
# but the app can't do anything without Cognito login.

variable "project_name" { type = string }
variable "environment" { type = string }
variable "cloudfront_domain" {
  type        = string
  description = "CloudFront domain for callback URLs"
}
variable "admin_emails" {
  type        = list(string)
  description = "Email addresses for admin users"
}

variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  prefix       = "${var.environment}-${var.project_name}"
  callback_url = "https://${var.cloudfront_domain}"
}

# ── Cognito User Pool ────────────────────────────────────────────────

resource "aws_cognito_user_pool" "admin" {
  name = "${local.prefix}-admin-pool"
  tags = var.tags

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  user_pool_add_ons {
    advanced_security_mode = "ENFORCED"
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }
}

# ── User Pool Domain ─────────────────────────────────────────────────

resource "aws_cognito_user_pool_domain" "admin" {
  domain       = "${local.prefix}-admin"
  user_pool_id = aws_cognito_user_pool.admin.id
}

# ── User Pool Client ─────────────────────────────────────────────────

resource "aws_cognito_user_pool_client" "admin" {
  name         = "${local.prefix}-admin-client"
  user_pool_id = aws_cognito_user_pool.admin.id

  generate_secret = false

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = [local.callback_url]
  logout_urls   = [local.callback_url]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 24

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "hours"
  }

  prevent_user_existence_errors = "ENABLED"

  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]
}

# ── Identity Pool (federated identities) ─────────────────────────────

resource "aws_cognito_identity_pool" "admin" {
  identity_pool_name               = "${local.prefix}-admin-identity"
  allow_unauthenticated_identities = false
  tags                             = var.tags

  cognito_identity_providers {
    client_id               = aws_cognito_user_pool_client.admin.id
    provider_name           = aws_cognito_user_pool.admin.endpoint
    server_side_token_check = true
  }
}

# ── IAM Role for authenticated users ─────────────────────────────────

resource "aws_iam_role" "authenticated" {
  name = "${local.prefix}-cognito-authenticated"
  tags = var.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "cognito-identity.amazonaws.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "cognito-identity.amazonaws.com:aud" = aws_cognito_identity_pool.admin.id
        }
        "ForAnyValue:StringLike" = {
          "cognito-identity.amazonaws.com:amr" = "authenticated"
        }
      }
    }]
  })
}

# The authenticated Identity-Pool role intentionally grants NO permissions.
# Authorization is enforced by the API Gateway JWT authorizer +
# the backend's deny-by-default group checks (shared/auth.py), not by IAM —
# the frontend makes no SigV4-signed execute-api calls. An execute-api grant
# here would be misleading (it would not gate writes) and over-broad, so the role
# grants nothing. It exists only because the Identity Pool requires an
# authenticated role for its role attachment.
resource "aws_iam_role_policy" "authenticated" {
  name = "${local.prefix}-cognito-authenticated-policy"
  role = aws_iam_role.authenticated.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DenyAllByDefault"
        Effect   = "Deny"
        Action   = "*"
        Resource = "*"
      }
    ]
  })
}

resource "aws_cognito_identity_pool_roles_attachment" "admin" {
  identity_pool_id = aws_cognito_identity_pool.admin.id
  roles = {
    authenticated = aws_iam_role.authenticated.arn
  }
}

# ── User Pool Groups (authorization tiers) ───────────────────────────
# Groups back the deny-by-default authorization in the backend Lambda
# (shared/auth.py). Hierarchy: admin > operator > viewer.

resource "aws_cognito_user_group" "viewer" {
  name         = "viewer"
  user_pool_id = aws_cognito_user_pool.admin.id
  description  = "Read-only access to all console data"
  precedence   = 3
}

resource "aws_cognito_user_group" "operator" {
  name         = "operator"
  user_pool_id = aws_cognito_user_pool.admin.id
  description  = "Read plus mutate stacks (restart, trigger, send, bind)"
  precedence   = 2
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.admin.id
  description  = "Full control including destructive ops and user management"
  precedence   = 1
}

# ── Initial Admin User ───────────────────────────────────────────────

resource "aws_cognito_user" "admin" {
  for_each     = toset(var.admin_emails)
  user_pool_id = aws_cognito_user_pool.admin.id
  username     = each.value

  attributes = {
    email          = each.value
    email_verified = true
  }
}

# Assign every initial admin user to the admin group. This is the
# anti-lockout guarantee: the accounts provisioned via admin_emails always
# retain full access once the deny-by-default authorization goes live.
resource "aws_cognito_user_in_group" "admin_membership" {
  for_each     = toset(var.admin_emails)
  user_pool_id = aws_cognito_user_pool.admin.id
  group_name   = aws_cognito_user_group.admin.name
  username     = aws_cognito_user.admin[each.value].username
}

# ── Outputs ──────────────────────────────────────────────────────────

output "user_pool_id" {
  value = aws_cognito_user_pool.admin.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.admin.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.admin.id
}

output "identity_pool_id" {
  value = aws_cognito_identity_pool.admin.id
}

output "cognito_domain" {
  value = "https://${aws_cognito_user_pool_domain.admin.domain}.auth.us-east-1.amazoncognito.com"
}

output "authenticated_role_arn" {
  value = aws_iam_role.authenticated.arn
}

# Signals that authorization groups + admin membership exist. The api module
# depends on this so the enforcing Lambda is never deployed before the
# admin accounts are safely in the admin group (anti-lockout ordering).
output "authorization_ready" {
  value = join(",", [
    aws_cognito_user_group.viewer.name,
    aws_cognito_user_group.operator.name,
    aws_cognito_user_group.admin.name,
    join(",", [for m in aws_cognito_user_in_group.admin_membership : m.username]),
  ])
}

output "login_url" {
  value = "https://${aws_cognito_user_pool_domain.admin.domain}.auth.us-east-1.amazoncognito.com/login?client_id=${aws_cognito_user_pool_client.admin.id}&response_type=code&scope=openid+email+profile&redirect_uri=${local.callback_url}"
}
