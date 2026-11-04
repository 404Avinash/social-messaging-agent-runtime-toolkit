# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The backend's dependencies are installed for the Lambda, not the host.

The packaging step runs ``pip install -r requirements.txt`` on the deploying
machine. Without a target, pip resolves for the host's own Python and
platform: a host Python older than 3.10 finds no boto3 the requirements
accept (it needs Python 3.10 or later), and a macOS or arm64 host puts its
own PyYAML extension in a zip that runs on Linux x86_64.
"""

import re
from pathlib import Path

CONSOLE = Path(__file__).resolve().parents[3]
API_MODULE = CONSOLE / "infra-terraform" / "modules" / "api" / "main.tf"


def _block(text: str, header: str) -> str:
    """The body of the top-level Terraform block that opens with ``header``."""
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start : i + 1]
    raise AssertionError(f"unclosed block {header}")


def _lambda_target() -> tuple[str, str]:
    """The backend Lambda's Python version and CPU architecture."""
    text = API_MODULE.read_text()
    function = _block(text, 'resource "aws_lambda_function" "backend"')
    [version] = re.findall(r'runtime\s*=\s*"python(\d+\.\d+)"', function)
    [arch] = re.findall(r'architectures\s*=\s*\["(\w+)"\]', function) or ["x86_64"]
    # The Powertools layer is built for one runtime and architecture too.
    layer = re.search(r"AWSLambdaPowertoolsPythonV3-python(\d+)-(\w+):", text)
    assert layer and layer.groups() == (version.replace(".", ""), arch), layer
    return version, arch


def _assert_targets_the_lambda(command: str) -> None:
    version, arch = _lambda_target()
    for flag in (
        f"--python-version {version}",
        f"--platform manylinux2014_{arch}",
        "--implementation cp",
        "--only-binary=:all:",
    ):
        assert flag in command, f"{flag!r} missing from: {command}"


def test_terraform_installs_the_requirements_for_the_lambda():
    text = API_MODULE.read_text()
    deps = _block(text, 'resource "null_resource" "backend_deps"')
    [command] = [line for line in deps.splitlines() if "pip install" in line]
    _assert_targets_the_lambda(command)


def test_the_manual_redeploy_installs_them_the_same_way():
    agents = (CONSOLE / "AGENTS.md").read_text()
    [command] = [line for line in agents.splitlines() if "pip install -r" in line]
    _assert_targets_the_lambda(command)
