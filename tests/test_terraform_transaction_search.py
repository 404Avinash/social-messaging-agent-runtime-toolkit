# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The switch for X-Ray Transaction Search in infra-terraform.

Transaction Search is not a stack resource. ``modules/observability`` turns it
on with ``aws xray update-trace-segment-destination``, which sends every X-Ray
span in the account and Region to CloudWatch Logs, and nothing turns it off
again when the stack is destroyed. A deployer must be able to leave the account
setting alone, so the root has a variable for it, and the variable defaults to
on so that a stack deployed before it existed plans no change.

The setting is optional, so a deployer whose credentials may not change it must
still be able to deploy: a failed call warns and the apply goes on.

Static checks over the ``.tf`` sources, and runs of the provisioner's command
against a stand-in ``aws``: no AWS credentials and no plan.
"""

from __future__ import annotations

import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra-terraform"
OBSERVABILITY_TF = INFRA / "modules" / "observability" / "main.tf"

_SWITCH = "enable_transaction_search"
_GATED = f"var.{_SWITCH} ? 1 : 0"
_PROVISIONER = 'resource "null_resource" "enable_xray_transaction_search"'

_REGION = "us-east-1"
_CLI_ARGS = [
    "xray",
    "update-trace-segment-destination",
    "--destination",
    "CloudWatchLogs",
    "--region",
    _REGION,
]
# Terraform runs a local-exec with no `interpreter` as `/bin/sh -c`, and /bin/sh
# is dash on Debian and Ubuntu and bash on macOS: run the command in each.
_SHELLS = [sh for sh in ("/bin/sh", "/bin/dash", "/bin/bash") if Path(sh).exists()]


def _code(text: str) -> str:
    """*text* without full-line ``#`` comments, which quote what they explain."""
    return re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)


def _block(text: str, header: str) -> str:
    """The top-level block that starts with *header*, up to its closing brace."""
    assert header in text, f"no `{header}` block"
    start = text.index(header)
    return text[start : text.index("\n}\n", start) + 2]


def _attribute(block: str, name: str) -> str | None:
    match = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*$", block, re.M)
    return match.group(1) if match else None


def test_root_variable_is_a_bool_that_defaults_to_on() -> None:
    """Default on: the module enabled it unconditionally before the switch."""
    block = _block(_code((INFRA / "variables.tf").read_text()), f'variable "{_SWITCH}"')
    assert _attribute(block, "type") == "bool"
    assert _attribute(block, "default") == "true", (
        f"{_SWITCH} must default to true, or every stack deployed before the "
        "variable existed plans a destroy of the Transaction Search resources"
    )
    description = _attribute(block, "description") or ""
    assert "account" in description and "destroy" in description, (
        "the description must say the setting is account-wide and survives a destroy"
    )


def test_observability_module_gets_the_root_switch() -> None:
    block = _block(_code((INFRA / "main.tf").read_text()), 'module "observability"')
    assert _attribute(block, _SWITCH) == f"var.{_SWITCH}", (
        f"module.observability does not receive var.{_SWITCH}, so setting it "
        "to false would still change the account's trace destination"
    )


def test_the_account_setting_and_its_log_policy_follow_the_switch() -> None:
    """The CLI call and the policy that lets X-Ray write to aws/spans."""
    tf = _code(OBSERVABILITY_TF.read_text())
    for header in (
        'resource "null_resource" "enable_xray_transaction_search"',
        'resource "aws_cloudwatch_log_resource_policy" "xray_access"',
    ):
        assert _attribute(_block(tf, header), "count") == _GATED, (
            f"{header} is not gated by var.{_SWITCH}"
        )


def test_only_the_gated_resource_changes_the_trace_destination() -> None:
    """A second call elsewhere would ignore the switch."""
    callers = [
        str(tf.relative_to(REPO_ROOT))
        for tf in sorted(INFRA.rglob("*.tf"))
        if ".terraform" not in tf.parts
        and "update-trace-segment-destination" in _code(tf.read_text())
    ]
    assert callers == [str(OBSERVABILITY_TF.relative_to(REPO_ROOT))]
    resource = _block(
        _code(OBSERVABILITY_TF.read_text()),
        'resource "null_resource" "enable_xray_transaction_search"',
    )
    assert "update-trace-segment-destination" in resource


def test_the_module_reads_every_variable_it_declares() -> None:
    """An input that nothing reads looks like a setting and changes nothing."""
    tf = _code(OBSERVABILITY_TF.read_text())
    declared = re.findall(r'^variable "(\w+)"', tf, re.M)
    # Drop the declarations, whose validation blocks read their own variable.
    body = re.sub(r'^variable "\w+" \{(?:[^\n]*\}$|.*?^\}$)', "", tf, flags=re.M | re.S)
    unread = [name for name in declared if not re.search(rf"\bvar\.{name}\b", body)]
    assert declared and not unread, (
        f"modules/observability declares unread variables: {unread}"
    )


def _provisioner_command() -> str:
    """The local-exec command as the shell gets it, with var.region rendered."""
    block = _block(OBSERVABILITY_TF.read_text(), _PROVISIONER)
    assert "interpreter" not in block, "the tests below run the command with /bin/sh -c"
    heredoc = re.search(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", block, re.S)
    assert heredoc, "the local-exec command is not a <<-EOT heredoc"
    # <<- strips the common indent, as textwrap.dedent does.
    command = textwrap.dedent(heredoc.group(1)).replace("${var.region}", _REGION)
    assert "${" not in command and "%{" not in command, (
        "the command interpolates more than var.region; render it here too"
    )
    return command


def _apply(
    tmp_path: Path, shell: str, aws_exit: int | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the command with a stand-in `aws` that exits *aws_exit*, or none on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "aws-args"
    if aws_exit is not None:
        aws = bin_dir / "aws"
        aws.write_text(
            textwrap.dedent(f"""\
            #!/bin/sh
            printf '%s\\n' "$@" > "{calls}"
            if [ {aws_exit} -ne 0 ]; then
              echo "An error occurred (AccessDeniedException) when calling the UpdateTraceSegmentDestination operation" >&2
            fi
            exit {aws_exit}
            """)
        )
        aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    result = subprocess.run(
        [shell, "-c", _provisioner_command()],
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, calls


def test_the_command_does_not_hide_a_failure() -> None:
    command = _provisioner_command()
    for hider in ("|| true", "|| :", "/dev/null"):
        assert hider not in command, f"`{hider}` hides a failed call from the deployer"


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_refused_call_warns_and_the_apply_goes_on(tmp_path: Path, shell: str) -> None:
    """Credentials without xray:UpdateTraceSegmentDestination still deploy, and are told."""
    result, calls = _apply(tmp_path, shell, aws_exit=254)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().split() == _CLI_ARGS
    assert "AccessDeniedException" in result.stderr, (
        "the CLI's own error must still show"
    )
    assert "WARNING" in result.stderr and "WARNING" not in result.stdout
    assert f"aws {' '.join(_CLI_ARGS)}" in result.stderr, (
        "the warning must name the command"
    )
    assert _SWITCH in result.stderr and "docs/configuration.md" in result.stderr


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_missing_cli_warns_and_the_apply_goes_on(tmp_path: Path, shell: str) -> None:
    """No `aws` on PATH fails the same way, and is told the same way."""
    result, _ = _apply(tmp_path, shell, aws_exit=None)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert f"aws {' '.join(_CLI_ARGS)}" in result.stderr


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_successful_call_prints_no_warning(tmp_path: Path, shell: str) -> None:
    result, calls = _apply(tmp_path, shell, aws_exit=0)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().split() == _CLI_ARGS
    assert result.stderr == ""
