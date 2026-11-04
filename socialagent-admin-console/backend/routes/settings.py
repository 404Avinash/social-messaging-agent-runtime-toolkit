# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Settings routes: admin settings + send-message proxy."""
from __future__ import annotations

import json
import re

from aws_lambda_powertools import Logger

from shared.auth import require_group
from shared.aws import lambda_client, settings_table
from shared.http import error_response as _error, json_response as _json, request_id
from shared.registry import get_registry as _get_registry, template_prefix_for, template_base_prefix_for
from shared.resolver import app, current_body, from_legacy

logger = Logger(service="admin-console-backend", child=True)

_ARN = re.compile(r"arn:aws[a-z-]*:[^\s\"',]+")
_ACCOUNT_ID = re.compile(r"\b\d{12}\b")


def _safe_reason(detail: dict) -> str:
    """The notification Lambda's error, with ARNs and account ids masked.

    Operators need the reason (a template Meta rejected, a phone outside the
    allowed list); they do not need the role or resource identifiers boto
    puts in the same string.
    """
    error_type = str(detail.get("errorType") or "Error")[:80]
    message = _ACCOUNT_ID.sub("<account>", _ARN.sub("<arn>", str(detail.get("errorMessage") or "")))
    return f"{error_type}: {message[:300]}" if message else error_type


def handle_get_settings(_params, _body):
    try:
        resp = settings_table.get_item(Key={"pk": "SETTINGS", "sk": "admin"})
        item = resp.get("Item", {})
        return _json(200, {
            "awsProfile": item.get("awsProfile", "default"),
            "defaultRegion": item.get("defaultRegion", "us-east-1"),
            "slackWebhookUrl": item.get("slackWebhookUrl", ""),
            "notificationEmail": item.get("notificationEmail", ""),
            "degradationAlerts": item.get("degradationAlerts", True),
            "deploymentFailureAlerts": item.get("deploymentFailureAlerts", True),
        })
    except Exception:
        return _json(200, {"awsProfile": "default", "defaultRegion": "us-east-1", "slackWebhookUrl": "", "notificationEmail": "", "degradationAlerts": True, "deploymentFailureAlerts": True})


def handle_send_message(params, body):
    """Send a WhatsApp message by invoking the notification Lambda."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    notification_arn = config.get("outputs", {}).get("notification_lambda_arn", "")
    if not notification_arn:
        return _json(400, {"error": "No notification Lambda configured"})
    phone = body.get("phone", "")
    message_type = body.get("type", "text")  # text or template
    if not phone:
        return _json(400, {"error": "phone is required"})
    # memory_text and log_to_memory have the notification Lambda write the reply
    # to the conversation's memory, so the agent sees what the operator said.
    # This send is out-of-band (no agent turn is running), like EventContext.
    try:
        if message_type == "template":
            template_name = body.get("templateName", "")
            # Strip the stack's template prefix if present (provider adds it back)
            prefix = template_prefix_for(config)
            if template_name.startswith(prefix):
                template_name = template_name[len(prefix):]
            # Also strip the base prefix (e.g. "socialagent_" from "socialagent_reminder")
            base_prefix = template_base_prefix_for(config)
            if template_name.startswith(base_prefix):
                template_name = template_name[len(base_prefix):]
            payload = {
                "source": "socialagent",
                "operation": "send_template",
                "data": {
                    "phone": phone,
                    "template_name": template_name,
                    "language": body.get("language", "en"),
                    "params": body.get("parameters", []),
                    "log_to_memory": True,
                },
            }
        else:
            text = body.get("text", "")
            payload = {
                "source": "socialagent",
                "operation": "send_text",
                "data": {"phone": phone, "text": text, "memory_text": text},
            }
        resp = lambda_client.invoke(FunctionName=notification_arn, InvocationType="RequestResponse", Payload=json.dumps(payload))
        resp_payload = json.loads(resp["Payload"].read())
        # A handler that raised is reported in FunctionError, not as a transport
        # error, so the invoke itself "succeeds". Check it so anything Meta rejects
        # (an unapproved template, a parameter-count mismatch, free text outside
        # the 24h window) is reported as a failure rather than as a sent message.
        function_error = resp.get("FunctionError")
        status = resp.get("StatusCode", 200)
        if function_error or not 200 <= int(status) < 300:
            # A raised handler's payload is its exception: message, type and
            # stack trace, with ARNs and file paths. Log it; the browser gets
            # where to look. The notification Lambda logs the same error, so it
            # also shows in the stack's Errors tab.
            rid = request_id()
            detail = resp_payload if isinstance(resp_payload, dict) else {}
            logger.error("send_message_failed", extra={
                "request_id": rid,
                "function_error": function_error,
                "invoke_status": status,
                "error_type": detail.get("errorType"),
                "error_message": detail.get("errorMessage"),
            })
            return _json(502, {
                "sent": False,
                "phone": phone,
                "error": f"Send failed: {_safe_reason(detail)}",
                "requestId": rid,
            })
        return _json(200, {"sent": True, "phone": phone, "response": resp_payload})
    except Exception:
        return _error(500, "Send message failed")


def handle_update_settings(_params, body):
    # Keys last, so a body carrying pk/sk cannot redirect the write to another
    # item, such as the health-probe counter that routes/stacks.py subtracts from
    # Lambda invocations.
    item = {**(body or {}), "pk": "SETTINGS", "sk": "admin"}
    try:
        settings_table.put_item(Item=item)
    except Exception:
        # DynamoDB rejects floats, among others. Unhandled, API Gateway would
        # return a bodyless 502.
        return _error(400, "Could not save settings")
    return _json(200, {"status": "saved"})



@app.get("/api/settings")
@require_group("operator")
def get_settings():
    return from_legacy(handle_get_settings({}, {}))


@app.put("/api/settings")
@require_group("admin")
def update_settings():
    return from_legacy(handle_update_settings({}, current_body()))


@app.put("/api/stacks/<id>/send-message")
@require_group("operator")
def send_message(id: str):
    return from_legacy(handle_send_message({"id": id}, current_body()))
