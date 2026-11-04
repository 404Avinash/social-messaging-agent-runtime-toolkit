# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "aws_region" {
  description = "AWS region for backend resources."
  type        = string
  default     = "us-east-1"
}

variable "project_prefix" {
  description = "Unique prefix for all resources (e.g. 'socialagent')."
  type        = string
  default     = "socialagent"
}

variable "environments" {
  description = "List of environments to create backends for."
  type        = list(string)
  default     = ["dev", "prod"]
}
