# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A failed send must not be reported as delivered.

`handle_send_message` invokes the notification Lambda synchronously and returned
``{"sent": true}`` on any invocation that completed — including one that raised.
Lambda signals a handler exception with a ``FunctionError`` field, not a transport
error, so the call "succeeds" while nothing was delivered.

The operator-visible effect is a green "Message sent to <phone>" flash for a
message WhatsApp dropped: a Meta rejection for an unapproved template, a
parameter-count mismatch, or free text sent outside the 24-hour window.
"""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

_STACK = {
    "project": "socialagent",
    "environment": "dev",
    "outputs": {
        "notification_lambda_arn": "arn:aws:lambda:us-east-1:111122223333:function:n"
    },
}


def _send(lambda_response: dict, body: dict | None = None):
    """Drive handle_send_message with a stubbed Lambda client."""
    from routes import settings

    client = MagicMock()
    client.invoke.return_value = lambda_response

    original_client = settings.lambda_client
    original_registry = settings._get_registry
    settings.lambda_client = client
    settings._get_registry = lambda _id: _STACK
    try:
        return settings.handle_send_message(
            {"id": "socialagent-dev"}, body or {"phone": "+15555550100", "text": "hola"}
        )
    finally:
        settings.lambda_client = original_client
        settings._get_registry = original_registry


def _payload(obj) -> io.BytesIO:
    return io.BytesIO(json.dumps(obj).encode())


def test_handler_exception_is_not_reported_as_sent(caplog):
    """FunctionError means the notification Lambda raised — nothing was sent."""
    resp = _send(
        {
            "FunctionError": "Unhandled",
            "Payload": _payload(
                {
                    "errorMessage": "Template does not exist",
                    "errorType": "ClientError",
                    "stackTrace": ['  File "/var/task/handler.py", line 1'],
                }
            ),
        }
    )

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body.get("sent") is not True
    # The operator sees the reason; the stack trace stays in the log.
    assert body["error"] == "Send failed: ClientError: Template does not exist"
    assert "stackTrace" not in resp["body"] and "/var/task" not in resp["body"]
    logged = [r for r in caplog.records if r.getMessage() == "send_message_failed"]
    assert logged and logged[0].error_message == "Template does not exist"


def test_successful_send_is_still_reported_as_sent():
    resp = _send({"Payload": _payload({"ok": True})})

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["sent"] is True


def test_non_200_invocation_status_is_not_reported_as_sent():
    """A non-2xx StatusCode from Lambda is a failed invocation too."""
    resp = _send({"StatusCode": 500, "Payload": _payload({})})

    assert resp["statusCode"] >= 400
    assert json.loads(resp["body"]).get("sent") is not True


def test_failure_reason_masks_arns_and_account_ids():
    """boto puts role ARNs and the account id in the same message as the reason."""
    resp = _send(
        {
            "FunctionError": "Unhandled",
            "Payload": _payload(
                {
                    "errorType": "ClientError",
                    "errorMessage": (
                        "User: arn:aws:sts::111122223333:assumed-role/dev-notification/x is not "
                        "authorized to perform: social-messaging:SendWhatsAppMessage on account 111122223333"
                    ),
                }
            ),
        }
    )

    body = json.loads(resp["body"])
    assert "111122223333" not in resp["body"]
    assert "arn:aws" not in resp["body"]
    assert body["error"].startswith("Send failed: ClientError: User: <arn> is not authorized")


# ── a console reply is part of the conversation ───────────────────────────────
#
# The notification Lambda writes a send_text to AgentCore Memory only when the
# payload carries memory_text, and a send_template only with log_to_memory.
# Neither was set, so an operator's reply showed in the open transcript, was
# gone when the conversation was reopened, and the agent answered the user's
# next message without it. The console sends outside any agent turn, like
# EventContext, so the memory write cannot split a toolUse from its toolResult.

NOTIFICATION_HANDLER = Path(__file__).resolve().parents[4] / "src" / "socialagent" / "handlers" / "notification.py"


def _sent_payload(body: dict) -> dict:
    """The notification Lambda payload handle_send_message builds for ``body``."""
    from routes import settings

    client = MagicMock()
    client.invoke.return_value = {"Payload": _payload({"ok": True})}
    original_client, original_registry = settings.lambda_client, settings._get_registry
    settings.lambda_client = client
    settings._get_registry = lambda _id: _STACK
    try:
        resp = settings.handle_send_message({"id": "socialagent-dev"}, body)
    finally:
        settings.lambda_client, settings._get_registry = original_client, original_registry
    assert resp["statusCode"] == 200
    return json.loads(client.invoke.call_args.kwargs["Payload"])


def test_a_text_reply_is_written_to_the_conversation():
    payload = _sent_payload({"phone": "+15555550100", "text": "Your order ships tomorrow"})

    assert payload["operation"] == "send_text"
    assert payload["data"]["text"] == "Your order ships tomorrow"
    assert payload["data"]["memory_text"] == "Your order ships tomorrow"


def test_a_template_reply_is_written_to_the_conversation():
    payload = _sent_payload(
        {"phone": "+15555550100", "type": "template", "templateName": "order_update", "parameters": ["A-1"]}
    )

    assert payload["operation"] == "send_template"
    assert payload["data"]["log_to_memory"] is True


def test_the_notification_handler_still_reads_those_fields():
    source = NOTIFICATION_HANDLER.read_text()

    assert 'data.get("memory_text"' in source
    assert 'data.get("log_to_memory")' in source
