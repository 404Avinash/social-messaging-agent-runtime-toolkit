# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A stack's last deploy is read from the CodeBuild project that built it.

``_stack_status`` read the last deploy with the console's own CodeBuild
client and always from ``<prefix>-agent-build``, so a zip stack (its builds
live in ``<prefix>-agent-zip-build``) and a stack deployed in another Region
both read as never deployed: the Stack Detail header, the Overview's "Last
deploy" column and the Stacks page's "Last deploy" column showed "-" while
the Deploys tab listed the builds. The read now goes through
``_deploys_builder``, the same helper the Deploys tab uses (#62, #142).
"""

from unittest.mock import MagicMock

import pytest

CONSOLE_REGION = "us-east-1"
ELSEWHERE = "eu-west-1"

WEBHOOK = "dev-bot-webhook"

STACK = {
    "project": "bot",
    "environment": "dev",
    "region": CONSOLE_REGION,
    "deployment_type": "docker",
    "outputs": {"webhook_lambda": WEBHOOK},
    # The registry reads that discover the tools and the skills are skipped when
    # the stack has them, which every deployed stack does.
    "tools": ["send_message"],
    "skills": ["greetings"],
}
ZIP_STACK = {**STACK, "deployment_type": "zip"}
ELSEWHERE_STACK = {**STACK, "region": ELSEWHERE}


class FakeCodeBuild:
    """The two CodeBuild calls the last-deploy read makes, and what each answers."""

    def __init__(self, ids=None, builds=None):
        self.ids = [] if ids is None else ids
        self.builds = [] if builds is None else builds
        self.calls = []

    def list_builds_for_project(self, **kw):
        self.calls.append(("list_builds_for_project", kw))
        return {"ids": self.ids}

    def batch_get_builds(self, **kw):
        self.calls.append(("batch_get_builds", kw))
        return {"builds": self.builds}


@pytest.fixture
def stacks(monkeypatch):
    """``routes.stacks`` with the console's Region pinned, as test_deploys_unreadable does."""
    from routes import stacks as module

    monkeypatch.setattr(module, "REGION", CONSOLE_REGION)
    return module


@pytest.fixture
def status(stacks, monkeypatch):
    """``_stack_status`` over a stack, with every other read stubbed out."""

    def _status(config: dict) -> dict:
        monkeypatch.setattr(
            stacks,
            "_lambda_metrics",
            lambda names: [{"name": "webhook", "userInvocations": 0, "status": "healthy"}],
        )
        monkeypatch.setattr(stacks, "s3", MagicMock())
        monkeypatch.setattr(stacks.boto3, "client", lambda *a, **k: MagicMock())
        return stacks._stack_status(config)

    return _status


def _regions(monkeypatch, stacks, fakes: dict) -> list:
    """Pin ``client(service, region)`` and record every region it was asked for."""
    asked = []

    def client(service, region):
        asked.append((service, region))
        return fakes.setdefault(region, FakeCodeBuild())

    monkeypatch.setattr(stacks, "client", client)
    return asked


def test_a_zip_stack_is_read_from_its_zip_build_project(stacks, status, monkeypatch):
    """A zip stack is built in ``<prefix>-agent-zip-build``, not the docker one."""
    fake = FakeCodeBuild(ids=["dev-bot-agent-zip-build:abc"])
    monkeypatch.setattr(stacks, "codebuild", fake)

    status(ZIP_STACK)

    assert fake.calls[0][1]["projectName"] == "dev-bot-agent-zip-build"


def test_a_docker_stack_is_read_from_its_docker_build_project(stacks, status, monkeypatch):
    fake = FakeCodeBuild(ids=["dev-bot-agent-build:abc"])
    monkeypatch.setattr(stacks, "codebuild", fake)

    status(STACK)

    assert fake.calls[0][1]["projectName"] == "dev-bot-agent-build"


def test_a_stack_in_another_region_gets_its_own_codebuild_client(stacks, status, monkeypatch):
    """The console's client reads the console's Region, so a stack elsewhere read as empty."""
    monkeypatch.setattr(stacks, "codebuild", FakeCodeBuild())
    fakes = {}
    asked = _regions(monkeypatch, stacks, fakes)
    fakes[ELSEWHERE] = FakeCodeBuild(
        ids=["dev-bot-agent-build:abc"],
        builds=[{"id": "dev-bot-agent-build:abc", "endTime": "2026-11-01T10:00:00Z"}],
    )

    result = status(ELSEWHERE_STACK)

    assert asked == [("codebuild", ELSEWHERE)]
    assert result["lastDeploy"] == "2026-11-01T10:00:00Z"


def test_a_stack_in_the_console_region_keeps_the_module_client(stacks, status, monkeypatch):
    fake = FakeCodeBuild(ids=["dev-bot-agent-build:abc"])
    monkeypatch.setattr(stacks, "codebuild", fake)
    asked = _regions(monkeypatch, stacks, {})

    status(STACK)

    assert asked == []
    assert fake.calls[0][1]["projectName"] == "dev-bot-agent-build"


def test_the_last_deploy_is_the_build_that_ended_last(stacks, status, monkeypatch):
    """The newest build is the one read: its end time is the last deploy."""
    fake = FakeCodeBuild(
        ids=["dev-bot-agent-build:abc", "dev-bot-agent-build:def"],
        builds=[{"id": "dev-bot-agent-build:abc", "endTime": "2026-11-01T10:00:00Z"}],
    )
    monkeypatch.setattr(stacks, "codebuild", fake)

    result = status(STACK)

    assert fake.calls[0][1]["sortOrder"] == "DESCENDING"
    assert fake.calls[1][1]["ids"] == ["dev-bot-agent-build:abc"]
    assert result["lastDeploy"] == "2026-11-01T10:00:00Z"


def test_a_stack_with_no_builds_reads_no_deploy(stacks, status, monkeypatch):
    monkeypatch.setattr(stacks, "codebuild", FakeCodeBuild(ids=[]))

    result = status(STACK)

    assert result["lastDeploy"] == ""
