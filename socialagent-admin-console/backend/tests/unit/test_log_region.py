# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A stack's logs are read in the stack's region, not the console's.

The Errors tab, the Events feed and log search read every stack's log groups
with a client in the console's region, so a stack registered in another region
always read as having no errors and no events.
"""

import json
from datetime import UTC, datetime, timedelta

from tests.unit.fake_logs import FakeLogs, ms

ELSEWHERE = {
    "project": "bot",
    "environment": "dev",
    "region": "eu-west-1",
    "outputs": {"webhook_lambda": "dev-bot-webhook"},
}

# The console's Region in these tests. It is pinned, not read from the
# runner's AWS_REGION: with that set to the stack's Region, the routes read
# the stack with the console's own client and the tests checked nothing.
CONSOLE_REGION = "us-east-1"
assert CONSOLE_REGION != ELSEWHERE["region"]


def _clients(groups: dict) -> tuple[dict, callable]:
    fakes: dict[str, FakeLogs] = {}

    def client(service: str, region: str) -> FakeLogs:
        assert service == "logs"
        return fakes.setdefault(region, FakeLogs(groups))

    return fakes, client


def _recent(message: str) -> dict:
    return {"timestamp": ms(datetime.now(UTC) - timedelta(minutes=1)), "message": message}


def test_errors_tab_reads_the_stacks_region(monkeypatch):
    from routes import stacks

    fakes, client = _clients({"/aws/lambda/dev-bot-webhook": [_recent("[ERROR] boom")]})
    monkeypatch.setattr(stacks, "client", client)
    monkeypatch.setattr(stacks, "logs", FakeLogs({}))
    monkeypatch.setattr(stacks, "REGION", CONSOLE_REGION)
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: ELSEWHERE)

    rows = json.loads(stacks.handle_get_errors({"id": "bot-dev"}, {})["body"])["results"]

    assert [r["message"] for r in rows] == ["[ERROR] boom"]
    assert set(fakes) == {"eu-west-1"}


def test_events_feed_reads_the_stacks_region(monkeypatch):
    from routes import events, stacks

    line = _recent("SOCIALAGENT_EVENT " + json.dumps({"event_type": "order", "source": "shop"}))
    fakes, client = _clients({"/aws/lambda/dev-bot-event": [line]})
    monkeypatch.setattr(stacks, "client", client)
    monkeypatch.setattr(stacks, "logs", FakeLogs({}))
    # The events route reads through stacks._logs_reader, which compares
    # with stacks.REGION.
    monkeypatch.setattr(stacks, "REGION", CONSOLE_REGION)
    monkeypatch.setattr(events, "_get_registry", lambda _id: ELSEWHERE)

    feed = json.loads(events.handle_get_events({"id": "bot-dev"}, {})["body"])["results"]

    assert [e["eventType"] for e in feed] == ["order"]
    assert set(fakes) == {"eu-west-1"}


def test_log_search_reads_each_stack_in_its_region(monkeypatch):
    from routes import logs

    here = {**ELSEWHERE, "project": "home", "region": "us-east-1", "outputs": {"webhook_lambda": "dev-home-webhook"}}
    fakes, client = _clients({
        "/aws/lambda/dev-bot-webhook": [_recent("[INFO] from Ireland")],
        "/aws/lambda/dev-home-webhook": [_recent("[INFO] from Virginia")],
    })
    monkeypatch.setattr(logs, "client", client)
    monkeypatch.setattr(logs, "get_all_registries", lambda: {"bot-dev": ELSEWHERE, "home-dev": here})

    rows = json.loads(logs.search_logs_impl({"q": "", "minutes": 60})["body"])["results"]

    assert {r["message"] for r in rows} == {"[INFO] from Ireland", "[INFO] from Virginia"}
    read = {
        region: {c.kwargs["logGroupName"] for c in fake.filter_log_events.call_args_list}
        for region, fake in fakes.items()
    }
    assert "/aws/lambda/dev-bot-webhook" in read["eu-west-1"]
    assert "/aws/lambda/dev-bot-webhook" not in read["us-east-1"]
