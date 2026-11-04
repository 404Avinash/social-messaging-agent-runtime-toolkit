# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schedule handlers need the scheduler Lambda; the deploy says so.

With ``features.scheduler_lambda`` off, EventBridge targets the notification
Lambda, which routes by ``operation`` among the send operations and answers
``unknown operation`` to a schedule. Every ``@app.on_schedule`` handler in the
project is then dead code, and the schedule, if one-shot, deletes itself after
the failed invocation. The blueprints all ship such handlers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from socialagent.cli._deploy import schedule_handlers_without_lambda

REPO = Path(__file__).parent.parent


def test_every_blueprint_with_schedule_handlers_enables_the_scheduler_lambda():
    for bp in sorted((REPO / "blueprints").iterdir()):
        handler = bp / "handlers" / "scheduler.py"
        if not handler.exists() or "@app.on_schedule(" not in handler.read_text():
            continue
        config = yaml.safe_load((bp / "socialagent.dev.yaml").read_text())
        assert config.get("features", {}).get("scheduler_lambda") is True, (
            f"{bp.name} ships @app.on_schedule handlers but features.scheduler_lambda is "
            "not true, so they would never run on a default deploy"
        )


def _project(tmp_path: Path, handler_src: str, flag: bool | None) -> tuple[dict, Path]:
    (tmp_path / "handlers").mkdir()
    (tmp_path / "handlers" / "scheduler.py").write_text(handler_src)
    config: dict = {"project": "p", "environment": "dev", "region": "us-east-1"}
    if flag is not None:
        config["features"] = {"scheduler_lambda": flag}
    return config, tmp_path


def test_handlers_present_and_flag_off_is_reported(tmp_path):
    config, d = _project(
        tmp_path, '@app.on_schedule("visit_reminder")\nasync def h(d, c): ...\n', None
    )
    assert schedule_handlers_without_lambda(config, d) == ["visit_reminder"]


def test_flag_on_is_fine(tmp_path):
    config, d = _project(
        tmp_path, '@app.on_schedule("visit_reminder")\nasync def h(d, c): ...\n', True
    )
    assert schedule_handlers_without_lambda(config, d) == []


def test_no_handlers_is_fine_with_the_flag_off(tmp_path):
    config, d = _project(tmp_path, "# nothing registered\n", False)
    assert schedule_handlers_without_lambda(config, d) == []


def test_the_keyword_form_of_the_decorator_is_found(tmp_path):
    config, d = _project(
        tmp_path,
        '@app.on_schedule(name="visit_reminder")\nasync def h(d, c): ...\n',
        None,
    )
    assert schedule_handlers_without_lambda(config, d) == ["visit_reminder"]


def test_a_name_outside_lowercase_and_underscores_is_found(tmp_path):
    config, d = _project(
        tmp_path, '@app.on_schedule("visit-reminder2")\nasync def h(d, c): ...\n', None
    )
    assert schedule_handlers_without_lambda(config, d) == ["visit-reminder2"]


def test_handlers_registered_outside_scheduler_py_are_found(tmp_path):
    config, d = _project(tmp_path, "# nothing registered here\n", None)
    (d / "handlers" / "reminders.py").write_text(
        '@app.on_schedule("followup")\nasync def h(d, c): ...\n'
    )
    assert schedule_handlers_without_lambda(config, d) == ["followup"]


def test_an_app_object_with_another_name_is_found(tmp_path):
    config, d = _project(
        tmp_path,
        "bot = SocialAgentApp()\n"
        '@bot.on_schedule("followup")\nasync def h(d, c): ...\n',
        None,
    )
    assert schedule_handlers_without_lambda(config, d) == ["followup"]


def test_an_empty_handlers_dir_means_the_default(tmp_path):
    """An empty ``handlers_dir:`` loads as None, which the tfvars read as the
    default; the check must not raise on it."""
    config, d = _project(
        tmp_path, '@app.on_schedule("visit_reminder")\nasync def h(d, c): ...\n', None
    )
    config["agent"] = {"handlers_dir": None}
    assert schedule_handlers_without_lambda(config, d) == ["visit_reminder"]


def test_code_that_is_not_the_projects_own_is_skipped_in_the_handlers(tmp_path):
    """The handlers directory is walked like the rest of the project: hidden
    directories, virtualenvs and a vendored runtime are not the project's
    handlers, and a subpackage is."""
    config, d = _project(tmp_path, "# nothing registered here\n", None)
    for sub, name in (
        (".cache", "hidden"),
        ("venv", "venv"),
        ("socialagent_runtime", "vendored"),
        ("jobs", "followup"),
    ):
        (d / "handlers" / sub).mkdir()
        (d / "handlers" / sub / "m.py").write_text(
            f'@app.on_schedule("{name}")\nasync def h(d, c): ...\n'
        )
    assert schedule_handlers_without_lambda(config, d) == ["followup"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 0 directory")
def test_a_directory_that_cannot_be_read_is_skipped(tmp_path):
    config, d = _project(
        tmp_path, '@app.on_schedule("visit_reminder")\nasync def h(d, c): ...\n', None
    )
    (d / "handlers" / "data").mkdir()
    (d / "handlers" / "data").chmod(0)
    try:
        assert schedule_handlers_without_lambda(config, d) == ["visit_reminder"]
    finally:
        (d / "handlers" / "data").chmod(0o700)


def test_a_quoted_true_in_the_yaml_counts_as_on(tmp_path):
    config, d = _project(
        tmp_path, '@app.on_schedule("visit_reminder")\nasync def h(d, c): ...\n', None
    )
    config["features"] = {"scheduler_lambda": "true"}
    assert schedule_handlers_without_lambda(config, d) == []


def test_deploy_warns_but_does_not_block(tmp_path, capsys, monkeypatch):
    from socialagent.cli import _deploy

    config, d = _project(
        tmp_path, '@app.on_schedule("reminder")\nasync def h(d, c): ...\n', False
    )
    _deploy.warn_if_schedule_handlers_without_lambda(config, d)
    out = capsys.readouterr().out
    assert "reminder" in out and "scheduler_lambda" in out
