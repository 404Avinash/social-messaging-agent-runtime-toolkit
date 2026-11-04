# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DELETE /api/schedules/<name> tells a schedule already gone from a failure.

A one-shot schedule deletes itself once it fires, so one the console listed a
moment ago can be gone when an operator cancels it, in a bulk cancel or a
nuclear reset. The route answered 500 "Delete schedule failed" for that too,
and the console reported a failure where nothing was left to delete.
"""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.

NAME = "socialagent-bot-dev-reminder-0100"


@pytest.fixture
def scheduler(monkeypatch):
    from routes import schedules

    client = MagicMock()
    monkeypatch.setattr(schedules.boto3, "client", lambda *a, **k: client)
    return client


def _delete() -> tuple[int, dict]:
    from routes import schedules

    resp = schedules.handle_delete_schedule({"schedule_name": NAME}, {})
    return resp["statusCode"], json.loads(resp["body"])


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "DeleteSchedule")


def test_a_deleted_schedule_answers_200(scheduler):
    assert _delete() == (200, {"deleted": NAME})
    scheduler.delete_schedule.assert_called_once_with(Name=NAME)


def test_a_schedule_already_gone_answers_404(scheduler):
    scheduler.delete_schedule.side_effect = _client_error("ResourceNotFoundException")

    assert _delete() == (404, {"error": "Schedule not found"})


@pytest.mark.parametrize("error", [_client_error("AccessDeniedException"), RuntimeError("boom")])
def test_any_other_failure_is_still_a_500(scheduler, error):
    scheduler.delete_schedule.side_effect = error

    status, body = _delete()

    assert status == 500
    assert body["error"].startswith("Delete schedule failed")
