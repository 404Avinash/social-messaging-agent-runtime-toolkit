# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The GitLab ``terraform`` job runs the Terraform-backed tests on the Terraform
image (Alpine) with ``--noconftest`` and only the Python packages its ``apk add``
line installs, not the project's environment. A third-party import those test
files add without its Alpine package stops the job at collection, while every
other job, and a local run, still passes.

The ``terraform`` jobs run Terraform 1.11.4, for the mocked plan tests, so the
``terraform-minimum`` jobs validate the three roots on the minimum their
``required_version`` declares; nothing else would notice a construct newer than
that minimum."""

import ast
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GITLAB_CI = ROOT / ".gitlab-ci.yml"
GITHUB_CI = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"

# Where each Terraform root declares its required_version, and the -chdir the
# Makefile validates it from.
TERRAFORM_ROOTS = {
    "infra-terraform/versions.tf": "-chdir=$(TF_DIR) ",
    "infra-terraform/bootstrap/main.tf": "-chdir=$(TF_DIR)/bootstrap ",
    "socialagent-admin-console/infra-terraform/versions.tf": (
        "-chdir=$(UI_DIR)/infra-terraform "
    ),
}

# Alpine's package for each third-party module those tests may import. A new
# import needs an entry here and the package on the job's apk line.
ALPINE_PACKAGES = {"pytest": "py3-pytest", "yaml": "py3-yaml"}


def _terraform_job() -> dict:
    if not GITLAB_CI.exists():
        pytest.skip(".gitlab-ci.yml is export-ignored, so the published tree has none")
    return yaml.safe_load(GITLAB_CI.read_text(encoding="utf-8"))["terraform"]


def _third_party_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return {n for n in names if n not in sys.stdlib_module_names and n != "__future__"}


def test_terraform_job_installs_what_its_tests_import():
    job = _terraform_job()
    installed = set()
    for line in job["before_script"]:
        words = shlex.split(line)
        if words[:2] == ["apk", "add"]:
            installed.update(w for w in words[2:] if not w.startswith("-"))

    runs = [line for line in job["script"] if "pytest" in line]
    assert runs and all("--noconftest" in line for line in runs), (
        "the job runs pytest without the project's conftest"
    )
    test_files = sorted(
        {f for line in runs for f in re.findall(r"tests/\S+\.py", line)}
    )
    assert test_files, "the job's pytest lines name no test file"

    for test_file in test_files:
        for module in sorted(_third_party_imports(ROOT / test_file)):
            assert module in ALPINE_PACKAGES, (
                f"{test_file} imports {module}: add its Alpine package to "
                "ALPINE_PACKAGES and to the terraform job's apk line"
            )
            assert ALPINE_PACKAGES[module] in installed, (
                f"{test_file} imports {module}, but the terraform job does not "
                f"install {ALPINE_PACKAGES[module]}"
            )


def _declared_minimum() -> tuple[int, int]:
    """The major.minor every root's ``required_version = ">= ..."`` names."""
    floors = set()
    for path in TERRAFORM_ROOTS:
        text = (ROOT / path).read_text(encoding="utf-8")
        found = re.search(r'required_version\s*=\s*">=\s*(\d+)\.(\d+)', text)
        assert found, f"{path} declares no minimum Terraform version"
        floors.add((int(found[1]), int(found[2])))
    assert len(floors) == 1, f"the three roots declare different minimums: {floors}"
    return floors.pop()


def _recipe(target: str) -> list[str]:
    """The commands of a Makefile target, without its prerequisites'."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(f"{target}:")]
    assert starts, f"the Makefile has no {target} target"
    recipe = []
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("\t"):
            break
        if not line.lstrip().startswith("@#"):
            recipe.append(line.strip())
    return recipe


def _version(version: str) -> tuple[int, int]:
    major, minor, *_ = version.split(".")
    return int(major), int(minor)


def test_tf_validate_roots_validates_every_root_without_the_plan_tests():
    """``terraform test`` with ``override_during`` needs 1.11, so the target
    the minimum jobs run must not run it, and must still reach every root."""
    recipe = _recipe("tf-validate-roots")
    for path, chdir in TERRAFORM_ROOTS.items():
        for command in ("init -backend=false", "validate"):
            assert any(f"terraform {chdir}{command}" in line for line in recipe), (
                f"tf-validate-roots no longer runs {command} in the root of {path}"
            )
    assert not [line for line in recipe if re.search(r"terraform \S+ test\b", line)], (
        "tf-validate-roots runs terraform test, which needs Terraform 1.11"
    )
    full = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^tf-validate: tf-validate-roots\b", full, re.M), (
        "make tf-validate no longer validates the roots first"
    )
    assert any("test -test-directory=tftest" in line for line in _recipe("tf-validate"))


def test_gitlab_validates_the_roots_on_the_declared_minimum():
    if not GITLAB_CI.exists():
        pytest.skip(".gitlab-ci.yml is export-ignored, so the published tree has none")
    jobs = yaml.safe_load(GITLAB_CI.read_text(encoding="utf-8"))
    job = jobs["terraform-minimum"]
    image, _, version = job["image"]["name"].rpartition(":")
    assert image == jobs["terraform"]["image"]["name"].rpartition(":")[0]
    assert _version(version) == _declared_minimum(), (
        f"terraform-minimum runs Terraform {version}, but the roots declare "
        f">= {'.'.join(map(str, _declared_minimum()))}"
    )
    assert job["script"] == ["make tf-validate-roots"]


def test_github_validates_the_roots_on_the_declared_minimum():
    jobs = yaml.safe_load(GITHUB_CI.read_text(encoding="utf-8"))["jobs"]

    def setup_terraform(job: str) -> dict:
        (step,) = [
            s
            for s in jobs[job]["steps"]
            if s.get("uses", "").startswith("hashicorp/setup-terraform@")
        ]
        return step

    step = setup_terraform("terraform-minimum")
    assert step["uses"] == setup_terraform("terraform")["uses"]
    version = step["with"]["terraform_version"]
    assert _version(version) == _declared_minimum(), (
        f"terraform-minimum runs Terraform {version}, but the roots declare "
        f">= {'.'.join(map(str, _declared_minimum()))}"
    )
    runs = [s["run"] for s in jobs["terraform-minimum"]["steps"] if "run" in s]
    assert runs == ["make tf-validate-roots"]
