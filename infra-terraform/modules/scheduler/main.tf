# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# EventBridge Scheduler group

variable "project_name" { type = string }
variable "environment" { type = string }

locals { prefix = "${var.environment}-${var.project_name}" }

# Scheduler group scopes all SMART one-shot schedules
resource "aws_scheduler_schedule_group" "main" {
  name = "${local.prefix}-schedules"
}

output "schedule_group_name" { value = aws_scheduler_schedule_group.main.name }
