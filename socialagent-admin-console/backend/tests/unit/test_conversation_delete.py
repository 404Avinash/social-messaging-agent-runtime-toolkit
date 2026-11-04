# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deleting a conversation says so when events are left behind.

The route swallowed every failed DeleteEvent and every failed listing and
still answered 200 with the count it managed, so a throttled delete read
``{deleted: 0}`` and the console reported the conversation deleted while its
transcript was still there.
"""
import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

PHONE = "525555550100"
SESSION = f"whatsapp-session-conversation-{PHONE}"
REGISTRY = {
    "project": "bot",
    "environment": "dev",
    "region": "us-east-1",
    "outputs": {"memory_id": "bot_dev_memory-xyz"},
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


@pytest.fixture
def agentcore(monkeypatch):
    import routes.conversations as conversations

    client = MagicMock()

    def list_sessions(**kwargs):
        if kwargs["actorId"] != PHONE:
            # "+52..." is not a valid actor id; only one of the forms has to exist.
            raise _client_error("ValidationException")
        return {"sessionSummaries": [{"sessionId": SESSION}]}

    client.list_sessions.side_effect = list_sessions
    client.list_events.return_value = {"events": [{"eventId": "e1"}, {"eventId": "e2"}]}
    monkeypatch.setattr(conversations, "_get_registry", lambda _id: REGISTRY)
    monkeypatch.setattr(conversations.boto3, "client", lambda *a, **k: client)
    return client


def _delete(phone: str = PHONE) -> tuple[int, dict]:
    import routes.conversations as conversations

    resp = conversations.handle_delete_conversation({"id": "bot-dev", "phone_hash": phone}, {})
    return resp["statusCode"], json.loads(resp["body"])


def test_every_event_deleted_is_a_success(agentcore):
    status, body = _delete()

    assert status == 200
    assert body["deleted"] == 2


def test_a_conversation_already_gone_is_not_a_failure(agentcore):
    agentcore.list_events.return_value = {"events": []}

    status, body = _delete()

    assert status == 200
    assert body["deleted"] == 0


def test_failed_event_deletes_are_an_error_with_the_counts(agentcore):
    agentcore.delete_event.side_effect = _client_error("ThrottlingException")

    status, body = _delete()

    assert status == 502
    assert body["deleted"] == 0
    assert body["failed"] == 2
    assert "2 could not be deleted" in body["error"]
    assert "Throttling" not in body["error"]  # the AWS error text stays in the log


def test_a_partial_delete_is_an_error(agentcore):
    agentcore.delete_event.side_effect = [None, _client_error("ThrottlingException")]

    status, body = _delete()

    assert status == 502
    assert body["deleted"] == 1
    assert body["failed"] == 1


def test_sessions_that_cannot_be_listed_are_an_error(agentcore):
    agentcore.list_sessions.side_effect = _client_error("ThrottlingException")

    status, body = _delete()

    assert status == 502
    assert body["deleted"] == 0
    assert "could not be listed" in body["error"]


def test_events_that_cannot_be_listed_are_an_error(agentcore):
    agentcore.list_events.side_effect = _client_error("ThrottlingException")

    status, body = _delete()

    assert status == 502
    assert "could not be listed" in body["error"]


def test_an_e164_phone_deletes_through_its_digits(agentcore):
    # The "+52..." form is tried too and refused as an actor id; the digits
    # form, which SMART writes, is enough.
    status, body = _delete(f"+{PHONE}")

    assert status == 200
    assert body["deleted"] == 2


@pytest.mark.parametrize("phone, listed", [
    (PHONE, [PHONE]),
    (f"+{PHONE}", [PHONE, f"+{PHONE}"]),
])
def test_the_digits_form_is_listed_first_and_once(agentcore, phone, listed):
    # The two forms were a set, which iterates in string-hash order. That
    # order changes from one process to the next, so each cold start logged
    # a delete's failures in a different order.
    _delete(phone)

    assert [c.kwargs["actorId"] for c in agentcore.list_sessions.call_args_list] == listed


def test_a_stack_without_memory_has_nothing_to_delete(agentcore, monkeypatch):
    import routes.conversations as conversations

    monkeypatch.setattr(conversations, "_get_registry", lambda _id: {**REGISTRY, "outputs": {}})

    status, body = _delete()

    assert status == 200
    assert body["deleted"] == 0
    agentcore.list_sessions.assert_not_called()


def test_a_failed_call_is_logged_by_its_code_without_the_number(agentcore, caplog):
    # An AgentCore error message can quote the actor or session id, both built
    # from the user's number, so the log keeps the error's type and code only.
    listed = agentcore.list_sessions.side_effect

    def list_sessions(**kwargs):
        if kwargs["actorId"] != PHONE:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": f"Value '{kwargs['actorId']}' at 'actorId'"}},
                "ListSessions",
            )
        return listed(**kwargs)

    agentcore.list_sessions.side_effect = list_sessions
    agentcore.delete_event.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": f"Rate exceeded for {SESSION}"}}, "DeleteEvent"
    )
    caplog.set_level(logging.WARNING)

    status, body = _delete(f"+{PHONE}")

    assert status == 502
    assert body["failed"] == 2
    logged = [(r.getMessage(), getattr(r, "error_code", None)) for r in caplog.records]
    assert logged == [
        ("delete_event_failed", "ThrottlingException"),
        ("delete_event_failed", "ThrottlingException"),
        ("list_sessions_failed", "ValidationException"),
    ]
    for record in caplog.records:
        assert record.levelno == logging.WARNING and record.exc_info is None
        assert PHONE not in str(record.__dict__)
