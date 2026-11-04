# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Events routes: read event logs, trigger events."""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import UTC, datetime

import boto3

from aws_lambda_powertools import Logger

from routes.stacks import _log_read_response, _logs_reader, _newest_log_events
from shared.auth import require_group
from shared.aws import REGION
from shared.http import error_response as _error, json_response as _json, logged_failure
from shared.registry import get_registry as _get_registry
from shared.resolver import app, current_body, from_legacy

logger = Logger(service="admin-console-backend", child=True)

# The host a stack's Events API is reachable on: Terraform publishes the stage's
# invoke URL, <api-id>.execute-api.<region>.amazonaws.com.
_EVENT_API_HOST_RE = re.compile(r"(?P<api_id>[a-z0-9]+)\.execute-api\.(?P<region>[a-z0-9-]+)\.amazonaws\.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would replay the credential headers to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None

def handle_get_events(params, _body):
    """Return recent external events dispatched by the stack's event Lambda.

    The event Lambda (``src/socialagent/handlers/event.py``) emits one structured
    log line per invocation using the marker ``SOCIALAGENT_EVENT {json}``. We
    filter CloudWatch logs by that marker and parse the JSON to build a
    real event feed instead of returning raw Lambda logs.

    Answers ``{results, unreadable}`` like the other log routes
    (``_log_read_response``): a log group that does not exist yet (new stack,
    never triggered) or a window with no matching lines is no events, while a
    read that failed or ran out of calls names the Lambda in ``unreadable``,
    and is a 502 when it read nothing.
    """
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    log_group = f"/aws/lambda/{prefix}-event"
    stack_id = f"{config.get('project', 'unknown')}-{config.get('environment', 'unknown')}"
    lambda_fn = f"{prefix}-event"
    events: list[dict] = []
    # filter_log_events returns the oldest lines first, so one 24-hour page of
    # 200 showed a busy day's first events.
    read = _newest_log_events(log_group, "SOCIALAGENT_EVENT", hours=24, keep=200, client=_logs_reader(config))
    for e in read.events:
        msg = e.get("message", "")
        marker_idx = msg.find("SOCIALAGENT_EVENT ")
        if marker_idx < 0:
            continue
        try:
            parsed = json.loads(msg[marker_idx + len("SOCIALAGENT_EVENT "):].strip())
        except (json.JSONDecodeError, ValueError):
            continue
        events.append({
            "timestamp": str(datetime.fromtimestamp(e["timestamp"] / 1000, tz=UTC)),
            "stack": stack_id,
            "source": parsed.get("source") or "API Gateway",
            "eventType": parsed.get("event_type") or "(unknown)",
            "lambdaInvoked": lambda_fn,
            "payloadSummary": _format_event_summary(parsed),
            "status": parsed.get("status") or "success",
            "phone": parsed.get("phone", ""),
        })

    # Newest first so the UI matches CloudWatch expectations.
    events.sort(key=lambda x: x["timestamp"], reverse=True)
    unreadable = [{"lambda": lambda_fn, "reason": read.reason}] if read.reason else []
    return _log_read_response(events, unreadable, read.read_something())


def _apis_bound_to_key(apigw, api_key_id: str) -> set[str]:
    """REST API ids the API key may call: the API stages of its usage plans.

    Raises when the lookup fails, so the caller can tell an unverifiable
    binding from a URL the key is not bound to.
    """
    api_ids: set[str] = set()
    kwargs = {"keyId": api_key_id, "limit": 100}
    for _ in range(10):
        resp = apigw.get_usage_plans(**kwargs)
        for plan in resp.get("items", []):
            api_ids.update(s.get("apiId", "") for s in plan.get("apiStages", []))
        if not resp.get("position"):
            break
        kwargs["position"] = resp["position"]
    return api_ids


def handle_trigger_event(params, body):
    """Proxy an event to a stack's Events API endpoint.

    Sends the payload with both required credentials:
    - x-api-key (API Gateway usage plan)
    - x-webhook-secret (Lambda authorizer, from Secrets Manager)

    On credential-fetch failure we return ``412 Precondition Failed`` with
    a structured error instead of sending the request without the header
    and letting the admin UI show a generic ``403`` from API Gateway. This
    makes debugging a broken registry / IAM setup significantly faster.
    """
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    event_url = config.get("outputs", {}).get("event_api_url", "")
    if not event_url:
        return _json(400, {"error": "Stack has no event_api_url configured"})
    from urllib.parse import urlparse
    _parsed = urlparse(event_url)
    if _parsed.scheme != "https" or not _parsed.netloc:
        return _json(400, {"error": "event_api_url must be a valid HTTPS URL"})
    # The request below carries the stack's API key and webhook secret, so the
    # registry URL must name an API Gateway endpoint, not just any HTTPS host.
    # Matching the whole netloc also refuses a port or user@ prefix.
    host = _EVENT_API_HOST_RE.fullmatch(_parsed.netloc)
    if not host:
        return _json(400, {"error": "event_api_url must be an API Gateway execute-api endpoint"})
    region = config.get("region", REGION)
    if host.group("region") != region:
        return _json(400, {"error": "event_api_url must be in the stack's region"})
    # The host's shape alone would accept an execute-api endpoint in any AWS
    # account. A usage plan binds the registry's API key to its event API, so
    # the credentials go only to the API that key belongs to: never another
    # account's API, and never a URL the key is not bound to. The registry
    # itself stays a trust input (threat model A022).
    api_key_id = config.get("outputs", {}).get("event_api_key_id", "")
    if not api_key_id:
        # Without a key id the binding cannot be checked: refuse before any
        # credential is read. There is no account-wide get_api_keys fallback,
        # since reading every API key's value in the account is over-broad.
        return _json(200, {
            "status": 412,
            "response": json.dumps({
                "error": "Missing credentials for event API",
                "missing": ["x-api-key"],
                "details": ["event_api_key_id not found in stack registry outputs — re-run 'socialagent config sync'"],
            }, indent=2),
            "url": event_url,
            "credentialWarnings": [],
        })
    apigw = boto3.client("apigateway", region_name=region)
    try:
        bound = _apis_bound_to_key(apigw, api_key_id)
    except Exception:
        return _error(502, "Could not verify which API the stack's API key belongs to")
    if host.group("api_id") not in bound:
        return _json(400, {"error": "event_api_url is not the API this stack's API key belongs to"})
    payload = json.dumps(body.get("payload", {})).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    errors: list[str] = []

    # --- x-api-key (API Gateway usage plan) ---------------------------------
    api_key_value = ""
    try:
        key_resp = apigw.get_api_key(apiKey=api_key_id, includeValue=True)
        api_key_value = key_resp.get("value", "") or ""
    except Exception:
        logger.exception("get_api_key_failed", extra={"api_key_id": api_key_id})
        errors.append(f"get_api_key({api_key_id}) failed — check Lambda logs")
    if api_key_value:
        headers["x-api-key"] = api_key_value

    # --- x-webhook-secret (Lambda authorizer) -------------------------------
    sm = boto3.client("secretsmanager", region_name=region)
    secret_arn = config.get("outputs", {}).get("event_api_webhook_secret_arn", "")
    if not secret_arn:
        secret_name = f"{config.get('environment', '')}-{config.get('project', '')}-event-webhook-secret"
        try:
            secret_arn = sm.describe_secret(SecretId=secret_name).get("ARN", "")
        except Exception:
            logger.exception("describe_secret_failed", extra={"secret_name": secret_name})
            errors.append(f"describe_secret({secret_name}) failed — check Lambda logs")
    webhook_secret = ""
    if secret_arn:
        try:
            secret_val = sm.get_secret_value(SecretId=secret_arn).get("SecretString", "")
            parsed = json.loads(secret_val) if secret_val else {}
            webhook_secret = parsed.get("webhook_secret", "") if isinstance(parsed, dict) else ""
            if not webhook_secret:
                errors.append(f"secret {secret_arn} missing 'webhook_secret' field")
        except Exception:
            logger.exception("get_secret_value_failed")
            errors.append("get_secret_value failed — check Lambda logs")
    if webhook_secret:
        headers["x-webhook-secret"] = webhook_secret

    # Refuse to send without both credentials — API Gateway would reject
    # the request anyway, but returning a structured error is much more
    # useful than proxying the opaque 403. Keep the outer HTTP status 200
    # so the admin UI renders the diagnostic inline instead of bailing.
    if "x-api-key" not in headers or "x-webhook-secret" not in headers:
        missing = [h for h in ("x-api-key", "x-webhook-secret") if h not in headers]
        return _json(200, {
            "status": 412,
            "response": json.dumps({
                "error": "Missing credentials for event API",
                "missing": missing,
                "details": errors,
            }, indent=2),
            "url": event_url,
            "credentialWarnings": errors,
        })

    req = urllib.request.Request(event_url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            return _json(200, {"status": resp.status, "response": resp_body, "url": event_url, "credentialWarnings": errors})
    except urllib.error.HTTPError as e:
        return _json(200, {"status": e.code, "response": e.read().decode("utf-8"), "url": event_url, "credentialWarnings": errors})
    except Exception:
        return _json(200, {"status": 0, "response": logged_failure("Event trigger failed"), "url": event_url, "credentialWarnings": errors})


def _format_event_summary(parsed: dict) -> str:
    """Build a human-friendly summary string for the admin UI."""
    phone = parsed.get("phone") or ""
    summary = parsed.get("summary") or ""
    bits: list[str] = []
    if phone:
        bits.append(f"phone={phone}")
    if summary:
        bits.append(summary)
    return " • ".join(bits) if bits else "(no payload)"



@app.get("/api/stacks/<id>/events")
@require_group("operator")
def get_events(id: str):
    return from_legacy(handle_get_events({"id": id}, {}))


@app.put("/api/stacks/<id>/trigger-event")
@require_group("operator")
def trigger_event(id: str):
    return from_legacy(handle_trigger_event({"id": id}, current_body()))


