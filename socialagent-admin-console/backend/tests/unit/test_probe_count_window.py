# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Health probes are subtracted from the same 24 hours as the invocations.

Every console page load invokes each stack Lambda with a health probe, and those
probes count in the Lambda's Invocations metric. "Messages (24h)" was the
webhook's raw Invocations sum, so reloading the console raised it. The probe
counter meant to correct "Usage (24h)" was a single item whose ttl every probe
pushed forward, on a table with no TTL, so it counted every probe ever fired and
drove usage to zero. Probes are now counted per minute, and the minutes inside
the metric's window are subtracted, from usage and from the message count.
Lambda publishes Invocations a minute or more late, so the window ends a few
minutes back: the probe a page load just fired is not in the metric yet, and
subtracting it made each reload lower the count.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

WEBHOOK = "dev-bot-webhook"
START = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


@pytest.fixture
def table(monkeypatch):
    from routes import stacks

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="test-settings",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(stacks, "settings_table", t)
        yield t


def _metrics(monkeypatch, invocations: int, cw: MagicMock | None = None) -> dict:
    """One _lambda_metrics row for the webhook, with CloudWatch reporting ``invocations``."""
    from routes import stacks

    lam = MagicMock()
    lam.invoke.return_value = {"StatusCode": 200}
    lam.get_function.return_value = {"Configuration": {"LastModified": "2026-10-01"}}
    cw = cw or MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [{"Values": [invocations]}, {"Values": []}, {"Values": []}]
    }
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "cw", cw)
    return stacks._lambda_metrics([WEBHOOK])[0]


def test_a_reload_does_not_subtract_its_own_probe(table, monkeypatch):
    """Three real invocations today and three reloads in one minute.

    Each load subtracted the probe it had just fired, and the ones before it,
    which the metric did not yet hold: the count read 2, then 1, then 0.
    """
    for second in (0, 20, 40):
        with freeze_time(START + timedelta(seconds=second)):
            row = _metrics(monkeypatch, invocations=3)
        assert (row["probeInvocations"], row["userInvocations"]) == (0, 3)


def test_the_metric_and_the_probe_counters_end_together(table, monkeypatch):
    from routes import stacks

    cw = MagicMock()
    with freeze_time(START + timedelta(seconds=50)):
        _metrics(monkeypatch, invocations=3, cw=cw)

    kwargs = cw.get_metric_data.call_args.kwargs
    lag = timedelta(minutes=stacks._METRIC_LAG_MINUTES)
    assert kwargs["EndTime"] == START - lag
    assert kwargs["StartTime"] == START - lag - timedelta(hours=24)


def test_probes_older_than_the_window_are_not_subtracted(table, monkeypatch):
    for hours in (0, 1, 2):
        with freeze_time(START + timedelta(hours=hours)):
            _metrics(monkeypatch, invocations=3)

    # Two days later: 5 invocations in the last 24 hours, none of them a probe
    # the metric holds yet.
    with freeze_time(START + timedelta(days=2)):
        row = _metrics(monkeypatch, invocations=5)

    assert row["probeInvocations"] == 0
    assert row["userInvocations"] == 5


def test_probes_inside_the_window_are_all_subtracted(table, monkeypatch):
    for hours in (0, 5, 23):
        with freeze_time(START + timedelta(hours=hours)):
            row = _metrics(monkeypatch, invocations=10)

    # The probes at 0 and 5 hours; the one at 23 hours is not in the metric yet.
    assert row["probeInvocations"] == 2
    assert row["userInvocations"] == 8


def test_probes_earlier_in_the_hour_the_window_starts_in_are_not_subtracted(table, monkeypatch):
    """Ten reloads at 09:30-09:39, then a look a day later at 09:50.

    The metric window starts at 09:47 the day before, so none of the ten
    probes are in it. Counted by the hour, all ten were subtracted from the 6
    invocations CloudWatch reported, and usage read 0 instead of 6.
    """
    for minute in range(10):
        with freeze_time(START + timedelta(minutes=minute)):
            _metrics(monkeypatch, invocations=minute + 1)

    with freeze_time(START + timedelta(days=1, minutes=20)):
        row = _metrics(monkeypatch, invocations=6)

    assert row["probeInvocations"] == 0
    assert row["userInvocations"] == 6


def test_probes_later_in_the_minute_the_window_starts_in_are_subtracted(table, monkeypatch):
    """The window starts on a whole minute, which the metric includes, so a
    probe later in that minute counts."""
    with freeze_time(START + timedelta(seconds=40)):
        _metrics(monkeypatch, invocations=1)

    with freeze_time(START + timedelta(days=1, minutes=3, seconds=20)):
        row = _metrics(monkeypatch, invocations=4)

    assert row["probeInvocations"] == 1
    assert row["userInvocations"] == 3


def test_probes_in_the_minute_the_window_ends_on_are_not_subtracted(table, monkeypatch):
    """The window ends on a whole minute, which the metric leaves out, so a
    probe in that minute is not in the metric yet. Read up to the end minute
    itself, it was subtracted from usage."""
    from routes import stacks

    with freeze_time(START):
        _metrics(monkeypatch, invocations=3)

    with freeze_time(START + timedelta(minutes=stacks._METRIC_LAG_MINUTES, seconds=10)):
        row = _metrics(monkeypatch, invocations=3)

    assert (row["probeInvocations"], row["userInvocations"]) == (0, 3)


def test_every_page_of_probe_counters_is_counted(monkeypatch):
    from routes import stacks

    last_sk = stacks._probe_counter_key(WEBHOOK, datetime(2026, 10, 1, 9, 30, tzinfo=UTC))
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [{"count": 2}], "LastEvaluatedKey": {"pk": "PROBE_COUNT", "sk": last_sk}},
        {"Items": [{"count": 3}]},
    ]
    monkeypatch.setattr(stacks, "settings_table", table)

    row = _metrics(monkeypatch, invocations=10)

    assert row["probeInvocations"] == 5
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"]["sk"] == last_sk


def test_messages_24h_leaves_out_the_consoles_own_probes(table, monkeypatch):
    from routes import stacks

    lambdas = [
        {"name": "webhook", "invocations": 12, "userInvocations": 9, "status": "healthy"},
        {"name": "notification", "invocations": 7, "userInvocations": 4, "status": "healthy"},
    ]
    monkeypatch.setattr(stacks, "_lambda_metrics", lambda names: lambdas)
    monkeypatch.setattr(stacks, "codebuild", MagicMock())
    monkeypatch.setattr(stacks, "s3", MagicMock())
    monkeypatch.setattr(stacks.boto3, "client", lambda *a, **k: MagicMock())

    status = stacks._stack_status({"project": "bot", "environment": "dev", "outputs": {"webhook_lambda": WEBHOOK}})

    assert status["messages24h"] == 9
    # No conversation count: computing one lists every actor in the memory.
    assert "activeConversations" not in status


def test_backend_role_may_query_the_probe_counters():
    tf = (Path(__file__).resolve().parents[3] / "infra-terraform" / "modules" / "api" / "main.tf").read_text()
    start = tf.index('Sid      = "SettingsQuery"')
    statement = tf[start : tf.index("\n      },", start)]
    assert '"dynamodb:Query"' in statement
    assert "aws_dynamodb_table.settings.arn" in statement
    assert "-state" not in statement


def test_the_settings_table_deletes_probe_counters_it_no_longer_reads():
    """A counter per Lambda per minute of page loads, kept forever, grew the
    table without bound. Only probe counters carry a ttl."""
    root = Path(__file__).resolve().parents[2]
    tf = (root.parent / "infra-terraform" / "modules" / "api" / "main.tf").read_text()
    start = tf.index('resource "aws_dynamodb_table" "settings"')
    table = tf[start : tf.index("\n}\n", start)]
    ttl = table[table.index("ttl {") :]
    assert 'attribute_name = "ttl"' in ttl[: ttl.index("}")]
    assert "enabled        = true" in ttl[: ttl.index("}")]

    writers = [
        path.name
        for path in (root / "routes").glob("*.py")
        if '"ttl"' in path.read_text() and "settings_table" in path.read_text()
    ]
    assert writers == ["stacks.py"]
