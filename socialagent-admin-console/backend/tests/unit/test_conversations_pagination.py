# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cursor-based pagination on the conversations endpoint.

Covers accepting cursor/page_size and including nextToken only when more
pages exist, for `handle_get_conversations` and the
`_list_conversations` helper in routes/conversations.py.
"""
import json
from unittest.mock import MagicMock

import pytest


REGISTRY = {
    "project": "socialagent",
    "environment": "dev",
    "region": "us-east-1",
    "outputs": {"memory_id": "dev_socialagent_memory-xyz"},
}


def _actor(phone: str) -> dict:
    return {"actorId": phone}


def _events_resp_with_one_event():
    return {"events": [{"eventTimestamp": "2026-05-01T00:00:00Z"}], "nextToken": None}


@pytest.fixture
def mock_agentcore(monkeypatch):
    """Patch boto3.client + _get_registry so handler talks to a fake AgentCore."""
    import routes.conversations as handler_mod

    client = MagicMock()
    # Every actor reports exactly one event so none are skipped as ghosts.
    client.list_events.return_value = _events_resp_with_one_event()

    monkeypatch.setattr(handler_mod, "_get_registry", lambda _id: REGISTRY)
    monkeypatch.setattr(handler_mod.boto3, "client", lambda *a, **k: client)
    return client


def test_default_page_size_is_25(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {"actorSummaries": [], "nextToken": None}
    handler_mod.handle_get_conversations({"id": "socialagent-dev"}, {})

    _, kwargs = mock_agentcore.list_actors.call_args
    assert kwargs["maxResults"] == 25
    assert "nextToken" not in kwargs  # no cursor passed on first page


def test_page_size_query_param_overrides_default(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {"actorSummaries": [], "nextToken": None}
    handler_mod.handle_get_conversations({"id": "socialagent-dev", "page_size": "10"}, {})

    _, kwargs = mock_agentcore.list_actors.call_args
    assert kwargs["maxResults"] == 10


def test_cursor_passed_through_as_next_token(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {"actorSummaries": [], "nextToken": None}
    handler_mod.handle_get_conversations({"id": "socialagent-dev", "cursor": "tok-abc"}, {})

    _, kwargs = mock_agentcore.list_actors.call_args
    assert kwargs["nextToken"] == "tok-abc"


def test_response_shape_is_object_with_conversations(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {
        "actorSummaries": [_actor("15555550100")],
        "nextToken": None,
    }
    resp = handler_mod.handle_get_conversations({"id": "socialagent-dev"}, {})

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert isinstance(body, dict)
    assert isinstance(body["conversations"], list)
    assert len(body["conversations"]) == 1
    assert body["conversations"][0]["fullPhone"] == "15555550100"


def test_next_token_included_when_more_pages_exist(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {
        "actorSummaries": [_actor("15555550100")],
        "nextToken": "next-page-token",
    }
    resp = handler_mod.handle_get_conversations({"id": "socialagent-dev"}, {})

    body = json.loads(resp["body"])
    assert body["nextToken"] == "next-page-token"


def test_next_token_omitted_on_last_page(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {
        "actorSummaries": [_actor("15555550100")],
        "nextToken": None,
    }
    resp = handler_mod.handle_get_conversations({"id": "socialagent-dev"}, {})

    body = json.loads(resp["body"])
    assert "nextToken" not in body


def test_stack_not_found_returns_404(monkeypatch):
    import routes.conversations as handler_mod

    monkeypatch.setattr(handler_mod, "_get_registry", lambda _id: None)
    resp = handler_mod.handle_get_conversations({"id": "missing"}, {})
    assert resp["statusCode"] == 404


def test_missing_memory_id_returns_empty_conversations(monkeypatch):
    import routes.conversations as handler_mod

    monkeypatch.setattr(
        handler_mod, "_get_registry",
        lambda _id: {"project": "socialagent", "environment": "dev", "outputs": {}},
    )
    resp = handler_mod.handle_get_conversations({"id": "socialagent-dev"}, {})
    body = json.loads(resp["body"])
    assert body == {"conversations": []}


def test_invalid_page_size_falls_back_to_default(mock_agentcore):
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {"actorSummaries": [], "nextToken": None}
    handler_mod.handle_get_conversations({"id": "socialagent-dev", "page_size": "abc"}, {})

    _, kwargs = mock_agentcore.list_actors.call_args
    assert kwargs["maxResults"] == 25


def test_page_size_clamped_to_service_max(mock_agentcore):
    """A page_size above AgentCore's cap must be clamped, not passed through.

    list_actors rejects maxResults > 100 with a ValidationException, which the
    handler's `except Exception: actors = []` swallows into a 200 with an empty
    list — indistinguishable from a stack that has no conversations.
    """
    import routes.conversations as handler_mod

    mock_agentcore.list_actors.return_value = {"actorSummaries": [], "nextToken": None}
    handler_mod.handle_get_conversations({"id": "socialagent-dev", "page_size": "1000"}, {})

    _, kwargs = mock_agentcore.list_actors.call_args
    assert kwargs["maxResults"] == 100
