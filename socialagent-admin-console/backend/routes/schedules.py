# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schedules routes: list, delete."""
from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from shared.auth import require_group
from shared.aws import REGION
from shared.http import error_response as _error, json_response as _json
from shared.registry import get_all_registries as _get_all_registries
from shared.resolver import app, from_legacy

def handle_list_schedules(_params, _body):
    """List all EventBridge Scheduler schedules and match to stacks."""
    try:
        scheduler_client = boto3.client("scheduler", region_name=REGION)
        # Build Lambda ARN -> stack_id map from registries
        registries = _get_all_registries()
        lambda_to_stack = {}
        for sid, cfg in registries.items():
            for key in ("webhook_lambda", "notification_lambda_arn"):
                val = cfg.get("outputs", {}).get(key, "")
                if val:
                    # Normalize to just function name
                    fn_name = val.split(":")[-1] if ":" in val else val
                    lambda_to_stack[fn_name] = sid
            # Also match scheduler Lambda
            project = cfg.get("project", "")
            env = cfg.get("environment", "")
            if project and env:
                lambda_to_stack[f"{env}-{project}-scheduler"] = sid

        schedules = []
        paginator = scheduler_client.get_paginator("list_schedules")
        for page in paginator.paginate(MaxResults=100):
            for s in page.get("Schedules", []):
                name = s.get("Name", "")
                # Get full details
                try:
                    detail = scheduler_client.get_schedule(Name=name)
                except Exception:
                    continue
                target = detail.get("Target", {})
                target_arn = target.get("Arn", "")
                target_fn = target_arn.split(":")[-1] if target_arn else ""
                input_raw = target.get("Input", "{}")
                try:
                    input_data = json.loads(input_raw)
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                # Match to stack
                stack_id = lambda_to_stack.get(target_fn, "")
                expression = detail.get("ScheduleExpression", "")
                is_cron = expression.startswith("cron(")
                is_at = expression.startswith("at(")
                schedules.append({
                    "name": name,
                    "state": detail.get("State", "UNKNOWN"),
                    "expression": expression,
                    # EventBridge omits the zone for an expression read in UTC.
                    "timezone": detail.get("ScheduleExpressionTimezone") or "UTC",
                    "type": "recurring" if is_cron else "one-shot" if is_at else "rate",
                    "stack": stack_id,
                    "targetArn": target_arn,
                    "operation": input_data.get("operation", input_data.get("source", "")),
                    "phone": input_data.get("phone", input_data.get("data", {}).get("phone", "")),
                    "payload": input_data,
                    "createdAt": str(detail.get("CreationDate", "")),
                    "lastModified": str(detail.get("LastModificationDate", "")),
                })
        return _json(200, schedules)
    except Exception:
        return _error(500, "List schedules failed")


def handle_delete_schedule(params, _body):
    """Delete an EventBridge Scheduler schedule.

    A one-shot schedule deletes itself once it fires, so one listed a moment
    ago can be gone by the time it is cancelled. That answers 404, which the
    console counts as not found, not as a failed delete.
    """
    name = params["schedule_name"]
    try:
        scheduler_client = boto3.client("scheduler", region_name=REGION)
        scheduler_client.delete_schedule(Name=name)
        return _json(200, {"deleted": name})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return _json(404, {"error": "Schedule not found"})
        return _error(500, "Delete schedule failed")
    except Exception:
        return _error(500, "Delete schedule failed")



@app.get("/api/schedules")
@require_group("operator")
def list_schedules():
    return from_legacy(handle_list_schedules({}, {}))


@app.delete("/api/schedules/<schedule_name>")
@require_group("admin")
def delete_schedule(schedule_name: str):
    return from_legacy(handle_delete_schedule({"schedule_name": schedule_name}, {}))
