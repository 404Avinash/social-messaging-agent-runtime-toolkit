# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "cloudfront_url" {
  description = "Admin console URL"
  value       = module.frontend.cloudfront_url
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidation)"
  value       = module.frontend.cloudfront_distribution_id
}

output "s3_bucket_name" {
  description = "S3 bucket hosting the admin console"
  value       = module.frontend.s3_bucket_name
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  value       = module.auth.user_pool_id
}

output "cognito_client_id" {
  description = "Cognito App Client ID"
  value       = module.auth.client_id
}

output "cognito_identity_pool_id" {
  description = "Cognito Identity Pool ID"
  value       = module.auth.identity_pool_id
}

output "api_url" {
  description = "Backend API URL"
  value       = module.api.api_url
}

output "registry_bucket" {
  description = "S3 bucket for stack registry (socialagent.yaml files)"
  value       = module.api.registry_bucket
}

output "deployment_summary" {
  description = "Summary of deployed admin console"
  value = {
    url             = module.frontend.cloudfront_url
    api_url         = module.api.api_url
    cognito_pool    = module.auth.user_pool_id
    cognito_client  = module.auth.client_id
    identity_pool   = module.auth.identity_pool_id
    registry_bucket = module.api.registry_bucket
    s3_bucket       = module.frontend.s3_bucket_name
    distribution_id = module.frontend.cloudfront_distribution_id
  }
}
