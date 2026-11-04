# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "project_name" {
  description = "Project name prefix for all resources."
  type        = string
  default     = "socialagent-admin"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "build_dir" {
  description = "Path to the Vite build output (dist/)."
  type        = string
  default     = "../dist"
}

variable "admin_emails" {
  description = "Email addresses for admin users (created in Cognito)."
  type        = list(string)
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}

variable "shared_runtime_bucket" {
  description = "Name of the shared socialagent-runtime bucket (created by infra-terraform/bootstrap/)."
  type        = string
}
