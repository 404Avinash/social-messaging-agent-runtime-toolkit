# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Bedrock Guardrails — configurable content safety

variable "project_name" { type = string }
variable "environment" { type = string }
variable "alarm_topic_arn" {
  type    = string
  default = ""
}

# Whether to create the intervention alarm. This has to be a plain bool rather than
# a `var.alarm_topic_arn != ""` test: the caller passes module.monitoring.alarm_topic_arn,
# and an SNS topic's ARN is unknown at plan time until the topic exists. Deriving `count`
# from it therefore fails with "Invalid count argument" on any plan where the topic is
# new — which is every from-scratch deploy, and every project_name change.
variable "enable_alarm" {
  type    = bool
  default = true
}

# The policy. The root's four guardrail_* policy variables document these and
# hold their defaults; the module has none, so there is one place to read them.
variable "content_filter_strength" {
  description = "Strength for the content and prompt-attack filters (NONE, LOW, MEDIUM, HIGH)."
  type        = string
}

variable "denied_topics" {
  description = "Denied topics: [{ name, definition, examples }]. Empty turns the topic policy off."
  type = list(object({
    name       = string
    definition = string
    examples   = list(string)
  }))
}

variable "blocked_input_message" {
  description = "Message returned when the guardrail blocks a prompt."
  type        = string
}

variable "blocked_output_message" {
  description = "Message returned when the guardrail blocks a model response."
  type        = string
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # Everything the guardrail evaluates. The resource reads its policy from here
  # and nowhere else, and the version's description carries a hash of it, so a
  # change to any of it publishes a new version (see the version below).
  # PROMPT_ATTACK is evaluated on input only, so its output strength is NONE.
  # Every topic sends its examples (the root requires 1 to 5): the attribute is
  # optional and computed, and Terraform matches topics by position, so a topic
  # that left them out would keep the examples its position held in state, and
  # the hash below would not describe the policy Bedrock evaluates.
  policy = {
    blocked_input_messaging   = var.blocked_input_message
    blocked_outputs_messaging = var.blocked_output_message
    content_filters = concat(
      [
        for filter in ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"] : {
          type            = filter
          input_strength  = var.content_filter_strength
          output_strength = var.content_filter_strength
        }
      ],
      [{ type = "PROMPT_ATTACK", input_strength = var.content_filter_strength, output_strength = "NONE" }],
    )
    denied_topics = [
      for topic in var.denied_topics : {
        name       = topic.name
        type       = "DENY"
        definition = topic.definition
        examples   = topic.examples
      }
    ]
    managed_word_lists = ["PROFANITY"]
    pii_entities = concat(
      [
        for entity in [
          "CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY",
          "US_SOCIAL_SECURITY_NUMBER", "US_BANK_ACCOUNT_NUMBER",
          "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "SWIFT_CODE", "PIN", "PASSWORD",
        ] : { type = entity, action = "BLOCK" }
      ],
      [{ type = "DRIVER_ID", action = "ANONYMIZE" }],
    )
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_bedrock_guardrail" "main" {
  name                      = "${local.prefix}-guardrail"
  description               = "Content safety guardrail for ${var.project_name}"
  blocked_input_messaging   = local.policy.blocked_input_messaging
  blocked_outputs_messaging = local.policy.blocked_outputs_messaging

  content_policy_config {
    dynamic "filters_config" {
      for_each = local.policy.content_filters
      content {
        type            = filters_config.value.type
        input_strength  = filters_config.value.input_strength
        output_strength = filters_config.value.output_strength
      }
    }
  }

  # The API rejects a topic policy with no topics, so none is sent then.
  dynamic "topic_policy_config" {
    for_each = length(local.policy.denied_topics) > 0 ? [local.policy.denied_topics] : []
    content {
      dynamic "topics_config" {
        for_each = topic_policy_config.value
        content {
          name       = topics_config.value.name
          type       = topics_config.value.type
          definition = topics_config.value.definition
          examples   = topics_config.value.examples
        }
      }
    }
  }

  word_policy_config {
    dynamic "managed_word_lists_config" {
      for_each = local.policy.managed_word_lists
      content {
        type = managed_word_lists_config.value
      }
    }
  }

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = local.policy.pii_entities
      content {
        type   = pii_entities_config.value.type
        action = pii_entities_config.value.action
      }
    }
  }
}

# The runtime pins this version (BEDROCK_GUARDRAIL_VERSION), and Bedrock serves
# a numbered version as it was when it was created: editing the guardrail
# changes only its DRAFT. This resource has no update, and the provider
# replaces it when its description changes, so the description carries the
# sha256 of local.policy, all 64 hex digits (78 of the 200 characters the
# description allows): a policy change creates a new version from the updated
# DRAFT (it references the guardrail, so it is created after the update), and
# a stack still on "Initial version" gets one from its current DRAFT.
# create_before_destroy points the runtime and the alarm at the new version
# before the old one is deleted. No skip_destroy: Bedrock allows 20 versions
# per guardrail, and every old one would be kept.
resource "aws_bedrock_guardrail_version" "main" {
  guardrail_arn = aws_bedrock_guardrail.main.guardrail_arn
  description   = "Policy sha256:${sha256(jsonencode(local.policy))}"

  lifecycle {
    create_before_destroy = true
  }
}

# Bedrock publishes InvocationsIntervened (0 or 1 per evaluation) per guardrail
# only with the GuardrailArn + GuardrailVersion dimension set, and an alarm
# matches one exact set, so this follows the version this module publishes.
# It watches this guardrail only: a guardrail of your own that
# terraform.guardrail names instead (the root's effective_guardrail_* locals)
# is yours to alarm on. See
# https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-guardrails-cw-metrics.html
resource "aws_cloudwatch_metric_alarm" "guardrail_interventions" {
  count               = var.enable_alarm ? 1 : 0
  alarm_name          = "${local.prefix}-guardrail-interventions"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "InvocationsIntervened"
  namespace           = "AWS/Bedrock/Guardrails"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Bedrock Guardrail intervention detected"
  alarm_actions       = [var.alarm_topic_arn]
  treat_missing_data  = "notBreaching"
  dimensions = {
    GuardrailArn     = aws_bedrock_guardrail.main.guardrail_arn
    GuardrailVersion = aws_bedrock_guardrail_version.main.version
  }
}

output "guardrail_id" { value = aws_bedrock_guardrail.main.guardrail_id }
output "guardrail_version" { value = aws_bedrock_guardrail_version.main.version }
output "guardrail_arn" { value = aws_bedrock_guardrail.main.guardrail_arn }
