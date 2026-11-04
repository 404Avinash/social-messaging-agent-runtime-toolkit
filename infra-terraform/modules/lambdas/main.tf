# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Lambda Module — All SMART Lambda functions
# Receives pre-built zips and layer ARN from packaging.tf

variable "project_name" {
  type = string
}

variable "enable_inbound_attachments" {
  description = "Have the webhook stage inbound images and documents in the media bucket and list them in the agent payload (INBOUND_ATTACHMENTS=1)."
  type        = bool
  default     = false
}

variable "environment" {
  type = string
}

# Packaging inputs (from packaging.tf)
variable "layer_arn" {
  description = "ARN of the SMART SDK Lambda layer."
  type        = string
}

variable "webhook_zip_path" {
  type = string
}

variable "webhook_zip_hash" {
  type = string
}

variable "notification_zip_path" {
  type = string
}

variable "notification_zip_hash" {
  type = string
}

variable "event_zip_path" {
  type = string
}

variable "event_zip_hash" {
  type = string
}

variable "enable_scheduler_lambda" {
  type    = bool
  default = false
}

variable "scheduler_zip_path" {
  type    = string
  default = ""
}

variable "scheduler_zip_hash" {
  type    = string
  default = ""
}

# IAM role ARNs
variable "webhook_role_arn" {
  type = string
}

variable "notification_role_arn" {
  type = string
}

variable "event_role_arn" {
  type = string
}

# Resource references
variable "sns_topic_arn" {
  description = "WhatsApp SNS topic ARN (webhook trigger)."
  type        = string
}

variable "memory_id" {
  type = string
}

variable "phone_number_id" {
  type    = string
  default = ""
}

variable "meta_phone_number_id" {
  description = "Meta phone number ID (numeric). When set, webhook filters SNS events for other phones (shared-WABA scenario)."
  type        = string
  default     = ""
}

variable "agent_runtime_arn" {
  description = "AgentCore Runtime ARN (for webhook handler)."
  type        = string
  default     = ""
}

variable "notification_service_arn_override" {
  description = "If set, used as NOTIFICATION_SERVICE_ARN. Otherwise self-references the notification Lambda."
  type        = string
  default     = ""
}

variable "scheduler_role_arn" {
  type    = string
  default = ""
}

variable "template_prefix" {
  type    = string
  default = ""
}

variable "templates_path_in_layer" {
  description = "Path to templates.yaml inside the Lambda layer mount."
  type        = string
  default     = ""
}

variable "state_table_name" {
  type    = string
  default = ""
}

variable "transcription_bucket" {
  type    = string
  default = ""
}

variable "transcription_prefix" {
  description = "S3 key prefix used by the transcriber for temporary audio uploads (e.g. 'stacks/scheduler-dev'). Enables IAM-scoped access to a shared runtime bucket."
  type        = string
  default     = ""
}

variable "conversation_logs_bucket" {
  type    = string
  default = ""
}

variable "media_prefix" {
  type        = string
  default     = ""
  description = "S3 key prefix under which this stack writes media/logs. Empty for per-stack buckets."
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "model_id" {
  description = "Bedrock model ID or inference profile for the webhook's image and PDF calls. Empty uses the blueprint default (handlers/shared/model.py)."
  type        = string
  default     = ""
}

variable "guardrail_id" {
  description = "Bedrock guardrail (id or ARN) the webhook passes on its image and PDF calls, the one the agent uses: the stack's, or the one terraform.guardrail names (local.effective_guardrail_id in the root). Empty passes none."
  type        = string
  default     = ""
}

variable "guardrail_version" {
  description = "Version of guardrail_id; empty is its draft."
  type        = string
  default     = ""
}

variable "rate_limit_per_minute" {
  description = "Max messages per user per minute. 0 disables rate limiting."
  type        = number
  default     = 0
}

variable "transcribe_languages" {
  description = "Transcribe language codes a voice note may be in; empty means identify among all."
  type        = list(string)
  default     = []
}

variable "error_msg_throttle" {
  type    = string
  default = ""
}

variable "error_msg_validation" {
  type    = string
  default = ""
}

variable "error_msg_generic" {
  type    = string
  default = ""
}

variable "error_msg_timeout" {
  type    = string
  default = ""
}

variable "message_buffering_enabled" {
  description = "Enable inbound message debounce buffering (text-only v1). Requires the state table."
  type        = bool
  default     = false
}

variable "buffer_window_seconds" {
  description = "Debounce window in seconds (SQS DelaySeconds; capped at 900)."
  type        = number
  default     = 5
}

variable "buffer_max_wait_seconds" {
  description = "Hard cap (seconds) from the first buffered message before forcing a flush."
  type        = number
  default     = 45
}

variable "buffer_ttl_seconds" {
  description = "TTL backstop (seconds) on buffer items so an abandoned buffer self-cleans."
  type        = number
  default     = 900
}

variable "outbound_gap_seconds" {
  description = "Seconds between consecutive messages sent to one user in one go."
  type        = number
  default     = 3
}

variable "log_level" {
  description = "Root log level for the Lambda handlers (DEBUG/INFO/WARNING/ERROR)."
  type        = string
  default     = "INFO"
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # Target of EventContext.schedule in the event and scheduler Lambdas; empty
  # without a scheduler Lambda, and the SDK then targets the notification one.
  # Built from the notification Lambda's ARN (same partition, region and
  # account): the scheduler's own environment cannot reference
  # aws_lambda_function.scheduler, and module.lambdas' depends_on defers the
  # data sources below to apply time, which would diff both Lambdas every plan.
  scheduler_lambda_arn = var.enable_scheduler_lambda ? "${split(":function:", aws_lambda_function.notification.arn)[0]}:function:${local.prefix}-scheduler" : ""

  common_env = merge(
    {
      ENVIRONMENT                      = var.environment
      MEMORY_ID                        = var.memory_id
      PHONE_NUMBER_ID                  = var.phone_number_id
      SOCIALAGENT_META_PHONE_NUMBER_ID = var.meta_phone_number_id
      AGENT_RUNTIME_ARN                = var.agent_runtime_arn
      SOCIALAGENT_PROVIDER             = "eum_social"
      TEMPLATE_PREFIX                  = var.template_prefix
      TEMPLATES_PATH                   = var.templates_path_in_layer
      STATE_TABLE_NAME                 = var.state_table_name
      MEDIA_BUCKET                     = var.conversation_logs_bucket
      MEDIA_PREFIX                     = var.media_prefix
      # The provider only uploads media to WhatsApp from this bucket and fails
      # closed without it, so every Lambda that sends needs it.
      CONVERSATION_LOGS_BUCKET = var.conversation_logs_bucket
      LOG_LEVEL                = var.log_level
      OUTBOUND_GAP_SECONDS     = tostring(var.outbound_gap_seconds)
    },
    var.error_msg_throttle != "" ? { ERROR_MSG_THROTTLE = var.error_msg_throttle } : {},
    var.error_msg_validation != "" ? { ERROR_MSG_VALIDATION = var.error_msg_validation } : {},
    var.error_msg_generic != "" ? { ERROR_MSG_GENERIC = var.error_msg_generic } : {},
    var.error_msg_timeout != "" ? { ERROR_MSG_TIMEOUT = var.error_msg_timeout } : {},
  )
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# =============================================================================
# DEAD LETTER QUEUES
# =============================================================================

resource "aws_sqs_queue" "webhook_dlq" {
  name                      = "${local.prefix}-webhook-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true    # SSE-SQS: encrypt failed payloads (may contain PII)
}

resource "aws_sqs_queue" "notification_dlq" {
  name                      = "${local.prefix}-notification-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "event_dlq" {
  name                      = "${local.prefix}-event-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "scheduler_dlq" {
  count                     = var.enable_scheduler_lambda ? 1 : 0
  name                      = "${local.prefix}-scheduler-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# =============================================================================
# WEBHOOK LAMBDA
# =============================================================================

resource "aws_lambda_function" "webhook" {
  function_name = "${local.prefix}-webhook"
  role          = var.webhook_role_arn
  handler       = "webhook.handler"
  runtime       = "python3.12"
  timeout       = 120
  memory_size   = 512

  filename         = var.webhook_zip_path
  source_code_hash = var.webhook_zip_hash
  layers           = [var.layer_arn]

  environment {
    variables = merge(local.common_env, {
      NOTIFICATION_SERVICE_ARN          = var.notification_service_arn_override != "" ? var.notification_service_arn_override : aws_lambda_function.notification.arn
      SCHEDULER_ROLE_ARN                = var.scheduler_role_arn
      SCHEDULER_PREFIX                  = local.prefix
      TRANSCRIPTION_BUCKET              = var.transcription_bucket
      TRANSCRIPTION_PREFIX              = var.transcription_prefix
      TRANSCRIBE_LANGUAGES              = join(",", var.transcribe_languages)
      SOCIALAGENT_RATE_LIMIT_PER_MINUTE = tostring(var.rate_limit_per_minute)
      MODEL_ID                          = var.model_id
      BEDROCK_GUARDRAIL_ID              = var.guardrail_id
      BEDROCK_GUARDRAIL_VERSION         = var.guardrail_version
      MESSAGE_BUFFERING_ENABLED         = tostring(var.message_buffering_enabled)
      INBOUND_ATTACHMENTS               = var.enable_inbound_attachments ? "1" : ""
      BUFFER_WINDOW_SECONDS             = tostring(var.buffer_window_seconds)
      BUFFER_MAX_WAIT_SECONDS           = tostring(var.buffer_max_wait_seconds)
      BUFFER_TTL_SECONDS                = tostring(var.buffer_ttl_seconds)
      BUFFER_QUEUE_URL                  = var.message_buffering_enabled ? aws_sqs_queue.message_buffer[0].url : ""

    })
  }

  tracing_config { mode = "Active" }
  dead_letter_config { target_arn = aws_sqs_queue.webhook_dlq.arn }
}

# SNS → webhook Lambda subscription
resource "aws_sns_topic_subscription" "webhook" {
  topic_arn = var.sns_topic_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.webhook.arn
}

resource "aws_lambda_permission" "sns_invoke_webhook" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = var.sns_topic_arn
}

# SNS invokes the webhook asynchronously, and Lambda retries a failed async
# invocation twice by default. Each retry runs the agent turn again (replies and
# tool side effects included), and the handler already answers the user when
# the runtime call or buffering a text fails, so a failure that escapes it goes
# to the DLQ instead of being replayed. The SQS buffer flush is a synchronous
# invoke this does not cover: SQS redelivers a flush whose invocation failed.
resource "aws_lambda_function_event_invoke_config" "webhook" {
  function_name          = aws_lambda_function.webhook.function_name
  maximum_retry_attempts = 0
}

# =============================================================================
# MESSAGE BUFFER QUEUE (optional — inbound debounce)
# =============================================================================
# The webhook schedules a delayed flush by sending to this queue with
# DelaySeconds = debounce window. SQS delivers it back to the webhook (second
# event source) after the delay, where the generation token decides whether to
# flush. SSE-SQS encryption at rest (no KMS key management needed).

resource "aws_sqs_queue" "message_buffer_dlq" {
  count                     = var.message_buffering_enabled ? 1 : 0
  name                      = "${local.prefix}-message-buffer-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "message_buffer" {
  count = var.message_buffering_enabled ? 1 : 0
  name  = "${local.prefix}-message-buffer"
  # Visibility timeout must exceed the webhook function timeout (120s) so a
  # slow flush isn't redelivered mid-processing.
  visibility_timeout_seconds = 150
  message_retention_seconds  = 3600
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.message_buffer_dlq[0].arn
    maxReceiveCount     = 5
  })

  lifecycle {
    precondition {
      condition     = var.state_table_name != ""
      error_message = "message_buffering_enabled requires the DynamoDB state table — set enable_state_table = true."
    }
  }
}

# SQS (delayed flush) → webhook Lambda. Second event source on the webhook; the
# handler routes SQS records to the buffer flush path.
resource "aws_lambda_event_source_mapping" "buffer_flush" {
  count            = var.message_buffering_enabled ? 1 : 0
  event_source_arn = aws_sqs_queue.message_buffer[0].arn
  function_name    = aws_lambda_function.webhook.arn
  batch_size       = 1
  enabled          = true
}

# =============================================================================
# NOTIFICATION LAMBDA
# =============================================================================

# No event invoke config, unlike the webhook and the scheduler: notify's sends
# reach this Lambda with InvocationType="Event" and rely on Lambda's two async
# retries. A schedule's built-in send also lands here when the stack has no
# scheduler Lambda; the handler records each such firing in the state table
# before it sends and keeps the record when the send fails, so a retry of it
# sends nothing (handlers/notification.py), which needs enable_state_table.

resource "aws_lambda_function" "notification" {
  function_name = "${local.prefix}-notification"
  role          = var.notification_role_arn
  handler       = "notification.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  filename         = var.notification_zip_path
  source_code_hash = var.notification_zip_hash
  layers           = [var.layer_arn]

  environment {
    variables = local.common_env
  }

  tracing_config { mode = "Active" }
  dead_letter_config { target_arn = aws_sqs_queue.notification_dlq.arn }
}

# =============================================================================
# EVENT LAMBDA
# =============================================================================

resource "aws_lambda_function" "event" {
  function_name = "${local.prefix}-event"
  role          = var.event_role_arn
  handler       = "event.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  filename         = var.event_zip_path
  source_code_hash = var.event_zip_hash
  layers           = [var.layer_arn]

  environment {
    variables = merge(local.common_env, {
      NOTIFICATION_SERVICE_ARN = aws_lambda_function.notification.arn
      SCHEDULER_LAMBDA_ARN     = local.scheduler_lambda_arn
      SCHEDULER_ROLE_ARN       = var.scheduler_role_arn
      SCHEDULER_PREFIX         = local.prefix
    })
  }

  tracing_config { mode = "Active" }
  dead_letter_config { target_arn = aws_sqs_queue.event_dlq.arn }
}

# =============================================================================
# SCHEDULER LAMBDA (optional)
# =============================================================================

resource "aws_lambda_function" "scheduler" {
  count = var.enable_scheduler_lambda ? 1 : 0

  function_name = "${local.prefix}-scheduler"
  role          = var.event_role_arn # same permissions as event handler
  handler       = "scheduler.handler"
  runtime       = "python3.12"
  # A scheduled invoke_agent task waits for a whole agent turn, as the webhook
  # does. runtime_call ends that wait RUNTIME_REPLY_RESERVE_SECONDS before this
  # timeout (socialagent.constants), so the result or the fallback still sends.
  timeout     = 120
  memory_size = 256

  filename         = var.scheduler_zip_path
  source_code_hash = var.scheduler_zip_hash
  layers           = [var.layer_arn]

  environment {
    variables = merge(local.common_env, {
      NOTIFICATION_SERVICE_ARN = aws_lambda_function.notification.arn
      AGENT_RUNTIME_ARN        = var.agent_runtime_arn
      MEMORY_ID                = var.memory_id
      SCHEDULER_LAMBDA_ARN     = local.scheduler_lambda_arn
      SCHEDULER_ROLE_ARN       = var.scheduler_role_arn
      SCHEDULER_PREFIX         = local.prefix
    })
  }

  tracing_config { mode = "Active" }
  dead_letter_config { target_arn = aws_sqs_queue.scheduler_dlq[0].arn }
}

# EventBridge Scheduler invokes the scheduler Lambda asynchronously, and Lambda
# retries a failed or timed-out async event twice by default. A retry re-runs
# the whole handler, so the user gets a late duplicate of whatever the first
# attempt had sent: the agent turn's result, or a template sent before a later
# step failed. Every send already gets the AWS SDK's retries on throttling and
# 5xx errors, and only the built-in sends (send_text, send_template,
# send_message and schedule_message's payloads) retry on top of that
# (handlers/scheduled.py); an @app.on_schedule handler gets nothing more, so a
# failure goes straight to the DLQ. This does not stop a duplicate: EventBridge
# Scheduler delivers at least once and the async queue can hand one event over
# twice. The handler records each firing in the state table before it runs it
# (handlers/scheduler_event.py), which needs enable_state_table.
resource "aws_lambda_function_event_invoke_config" "scheduler" {
  count = var.enable_scheduler_lambda ? 1 : 0

  function_name          = aws_lambda_function.scheduler[0].function_name
  maximum_retry_attempts = 0
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "webhook_arn" {
  value = aws_lambda_function.webhook.arn
}

output "webhook_function_name" {
  value = aws_lambda_function.webhook.function_name
}

output "notification_arn" {
  value = aws_lambda_function.notification.arn
}

output "notification_function_name" {
  value = aws_lambda_function.notification.function_name
}

output "event_arn" {
  value = aws_lambda_function.event.arn
}

output "event_function_name" {
  value = aws_lambda_function.event.function_name
}

output "all_function_names" {
  value = compact(concat(
    [
      aws_lambda_function.webhook.function_name,
      aws_lambda_function.notification.function_name,
      aws_lambda_function.event.function_name,
    ],
    var.enable_scheduler_lambda ? [aws_lambda_function.scheduler[0].function_name] : [],
  ))
}
