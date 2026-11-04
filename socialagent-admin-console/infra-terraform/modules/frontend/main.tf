# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# S3 + CloudFront + OAC — zero public bucket access.
# Auth is client-side (Cognito hosted UI). CloudFront serves the SPA shell.

variable "project_name" { type = string }
variable "environment" { type = string }
variable "build_dir" {
  type        = string
  description = "Path to the Vite build output (dist/)"
}
variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  prefix    = "${var.environment}-${var.project_name}"
  origin_id = "${local.prefix}-s3-origin"
}

data "aws_caller_identity" "current" {}

# ── S3 Bucket (private) ──────────────────────────────────────────────

resource "aws_s3_bucket" "site" {
  bucket        = "${local.prefix}-admin-console-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.environment != "prod"
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "site" {
  bucket = aws_s3_bucket.site.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.site.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.site.arn
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.site.arn,
          "${aws_s3_bucket.site.arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.site]
}

# ── CloudFront OAC ───────────────────────────────────────────────────

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${local.prefix}-admin-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ── CloudFront Distribution ──────────────────────────────────────────

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  comment             = "${local.prefix} admin console"
  tags                = var.tags

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = local.origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = local.origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
  }

  # SPA: serve index.html for 403/404 (hash routing)
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }

  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    # minimum_protocol_version only takes effect alongside a custom ACM
    # certificate. On the default *.cloudfront.net certificate CloudFront pins
    # the field to TLSv1 and silently discards anything else, so setting it here
    # would make `terraform plan` report a perpetual diff. Controlling the viewer
    # TLS floor means bringing a custom domain and certificate.
    cloudfront_default_certificate = true
  }
}

resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "${local.prefix}-security-headers"
  comment = "Security headers for admin console"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }
    content_type_options { override = true }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    xss_protection {
      mode_block = true
      override   = true
      protection = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    content_security_policy {
      # script-src drops 'unsafe-inline': the Vite build emits no inline
      # <script> (modulePreload.polyfill=false), so scripts load only from 'self'.
      # style-src keeps 'unsafe-inline' — Cloudscape/Amplify inject runtime inline
      # styles (not scripts), and removing it would break the UI.
      content_security_policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'"
      override                = true
    }
  }
}

# ── Upload build artifacts ───────────────────────────────────────────

locals {
  mime_types = {
    ".html" = "text/html"
    ".css"  = "text/css"
    ".js"   = "application/javascript"
    ".json" = "application/json"
    ".svg"  = "image/svg+xml"
    ".png"  = "image/png"
    ".ico"  = "image/x-icon"
    ".txt"  = "text/plain"
    ".map"  = "application/json"
  }
}

resource "aws_s3_object" "site_files" {
  for_each = fileset(var.build_dir, "**")

  bucket       = aws_s3_bucket.site.id
  key          = each.value
  source       = "${var.build_dir}/${each.value}"
  etag         = filemd5("${var.build_dir}/${each.value}")
  content_type = lookup(local.mime_types, regex("\\.[^.]+$", each.value), "application/octet-stream")
}

# ── Outputs ──────────────────────────────────────────────────────────

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.site.domain_name
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "s3_bucket_name" {
  value = aws_s3_bucket.site.id
}

output "s3_bucket_arn" {
  value = aws_s3_bucket.site.arn
}
