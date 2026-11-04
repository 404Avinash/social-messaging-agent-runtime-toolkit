# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Optional DynamoDB State Table
# Per-user key-value state with TTL

variable "project_name" { type = string }
variable "environment" { type = string }
variable "kms_key_arn" {
  type        = string
  default     = ""
  description = "ARN of the KMS key for table encryption. Empty uses the AWS-managed key (alias/aws/dynamodb); set a customer-managed CMK ARN to override."
}

locals { prefix = "${var.environment}-${var.project_name}" }

resource "aws_dynamodb_table" "state" {
  name         = "${local.prefix}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # enabled=true with no kms_key_arn uses the AWS-managed key (alias/aws/dynamodb);
  # a non-empty kms_key_arn selects a customer-managed CMK.
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn != "" ? var.kms_key_arn : null
  }
}

output "table_name" { value = aws_dynamodb_table.state.name }
output "table_arn" { value = aws_dynamodb_table.state.arn }
