# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Packaging tests — the CLI must work from a pip install, not just a checkout.

``socialagent init`` and ``socialagent recipe add`` copy data directories (``blueprints/``,
``recipes/``, ``packages/socialagent-runtime/``) that live at the repo root, outside the
``src/socialagent`` package. Every other test in this suite runs from the checkout,
where those paths resolve; a pip-installed CLI has no repo root above it, so a
wheel missing that data leaves ``socialagent init`` broken for every external user
while the whole suite stays green.

These tests close that gap: one asserts the built wheel carries the data, the
others install it into a throwaway venv and run the commands for real — and
then boot the project that ``socialagent init`` produced, because scaffolding a project
that cannot start is the same bug one step later.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from socialagent.cli.blueprints import _BLUEPRINT_NAMES
from socialagent.cli.dev import _DEV_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every blueprint's Dockerfile hardcodes `EXPOSE 8080` and health-checks
# `/ping`, so that is the readiness signal a booted agent gives. There is no
# port override, which is why the boot tests run one at a time.
_AGENT_PORT = 8080
_HEALTH_URL = f"http://127.0.0.1:{_AGENT_PORT}/ping"

# Enough for a cold import of strands + boto3 + opentelemetry on a loaded CI box.
_BOOT_TIMEOUT_S = 120

# Where force-include places each data dir inside the wheel, and the file that
# proves the whole tree came along rather than just an empty directory entry.
_EXPECTED_ASSETS = [
    ("socialagent/cli/_assets/blueprints", "minimal/agent.py"),
    ("socialagent/cli/_assets/recipes", "rich-messages/tool.py"),
    ("socialagent/cli/_assets/socialagent-runtime", "notify.py"),
]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build the wheel once and return its path."""
    out_dir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # `build` is not a declared dev dependency; fall back to uv, which is
        # what the Makefile and CI use.
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        pytest.skip(f"could not build wheel: {result.stderr[-400:]}")

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


@pytest.fixture(scope="module")
def installed_cli(built_wheel: Path, tmp_path_factory) -> Path:
    """A venv with the built wheel installed; returns its bin directory.

    Module-scoped because creating a venv and installing the wheel is the same
    work for every test here, and none of them mutate the install.
    """
    venv = tmp_path_factory.mktemp("cli") / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
    )
    bin_dir = _venv_bin(venv)
    subprocess.run(
        [str(bin_dir / "pip"), "install", "-q", str(built_wheel)],
        check=True,
        capture_output=True,
    )
    return bin_dir


@pytest.mark.parametrize("asset_dir,probe_file", _EXPECTED_ASSETS)
def test_wheel_contains_cli_data(built_wheel: Path, asset_dir: str, probe_file: str):
    """The wheel ships each data dir the CLI copies into new projects."""
    names = zipfile.ZipFile(built_wheel).namelist()
    assert f"{asset_dir}/{probe_file}" in names, (
        f"{asset_dir}/{probe_file} missing from the wheel — `socialagent init` will fail "
        f"on a pip install. Check [tool.hatch.build.targets.wheel.force-include]."
    )


def test_wheel_blueprints_cover_every_registered_name(built_wheel: Path):
    """Every blueprint the registry offers is actually in the wheel.

    Guards the case where a blueprint is added to ``_BLUEPRINT_NAMES`` but the
    force-include pattern misses it, so ``socialagent init --blueprint <new>`` fails
    only for pip-installed users.
    """
    from socialagent.cli.blueprints import _BLUEPRINT_NAMES

    names = zipfile.ZipFile(built_wheel).namelist()
    for blueprint in _BLUEPRINT_NAMES:
        prefix = f"socialagent/cli/_assets/blueprints/{blueprint}/"
        assert any(n.startswith(prefix) for n in names), (
            f"blueprint '{blueprint}' is registered but absent from the wheel"
        )


def test_template_sync_archive_bundles_every_force_include():
    """The template-sync CodeBuild source must carry every force-included tree.

    ``infra-terraform/modules/templates`` zips a source bundle and runs
    ``pip install .`` on it. hatchling resolves force-include paths while
    generating metadata, so a missing tree aborts the install with
    ``FileNotFoundError: Forced include not found`` and ``templates sync`` never
    runs — no template reaches Meta and every scheduled reminder silently falls
    back to plain text inside the 24-hour window.

    ``built_wheel`` cannot catch this: it builds from the checkout, where all
    three trees exist. Only the Terraform archive can omit them, so this asserts
    the two lists agree.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]

    root_tf = (REPO_ROOT / "infra-terraform" / "main.tf").read_text()
    block = re.search(r"force_included_dirs\s*=\s*\{(.*?)\n  \}", root_tf, re.DOTALL)
    assert block, (
        "force_included_dirs is gone from the templates module call in main.tf"
    )
    bundled = dict(re.findall(r'"([^"]+)"\s*=\s*"([^"]+)"', block.group(1)))

    assert set(bundled) == set(force_include), (
        "force_included_dirs in infra-terraform/main.tf is out of step with "
        "[tool.hatch.build.targets.wheel.force-include]: "
        f"missing {sorted(set(force_include) - set(bundled))}, "
        f"stale {sorted(set(bundled) - set(force_include))}"
    )

    for archive_path, source in bundled.items():
        on_disk = REPO_ROOT / source.replace("${path.root}/../", "")
        assert on_disk.is_dir(), (
            f"{archive_path} points at {source}, which does not exist"
        )


def _hcl_expression(text: str, name: str) -> str:
    """Return the bracketed expression assigned to ``name`` in HCL *text*.

    Brackets are balanced outside string literals, so the regexes the filters
    carry cannot end the expression early.
    """
    match = re.search(rf"^\s*{name}\s*=\s*", text, re.MULTILINE)
    assert match, f"no `{name} =` assignment"
    depth, in_string, i = 0, False, match.end()
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[match.end() : i + 1]
        i += 1
    raise AssertionError(f"unbalanced `{name}` expression")


def test_template_sync_archive_filters_match_the_layer():
    """The template-sync archive must drop what the layer archive drops.

    Both archives read the force-included trees with ``fileset()``, which sees
    the disk rather than git, so an untracked cache lands in the plan and a
    stray binary fails ``file()`` outright. ``packaging.tf`` explains the
    filters; this keeps the copy in ``modules/templates/main.tf`` in step, and
    inside the ``force_included_files`` expression that feeds the archive.
    """
    infra = REPO_ROOT / "infra-terraform"
    pattern = re.compile(r'regexall\("((?:[^"\\]|\\.)*)"')
    layer = pattern.findall(
        _hcl_expression((infra / "packaging.tf").read_text(), "layer_files")
    )
    templates = pattern.findall(
        _hcl_expression(
            (infra / "modules/templates/main.tf").read_text(), "force_included_files"
        )
    )

    assert layer, "the layer_files filters are gone from infra-terraform/packaging.tf"
    missing = sorted(set(layer) - set(templates))
    assert not missing, (
        "modules/templates/main.tf no longer applies these packaging.tf filters "
        f"to the force-included trees: {missing}"
    )


def test_layer_archive_carries_everything_the_build_reads():
    """The layer CodeBuild source must carry every path ``pip install .`` reads.

    ``infra-terraform/packaging.tf`` zips only ``layer_root_files`` and the
    files under ``layer_trees``. Omit a force-include tree and the layer build
    fails. Omit anything else and it quietly succeeds: without the package
    source the layer has no SDK, without a license file the wheel drops it, and
    without ``.gitignore`` hatchling ships gitignored files under
    ``src/socialagent`` in the layer. ``built_wheel`` builds from the checkout,
    where all of these exist, so it cannot notice.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    readme = pyproject["project"].get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    read_by_build = [
        *wheel["packages"],
        *wheel.get("force-include", {}),
        *pyproject["project"].get("license-files", []),
        *([readme] if readme else []),
        pyproject["tool"]["hatch"]["version"]["path"],  # metadata generation reads it
        ".gitignore",
    ]

    packaging_tf = (REPO_ROOT / "infra-terraform" / "packaging.tf").read_text()

    def tf_list(name: str) -> list[str]:
        match = re.search(rf"^\s*{name}\s*=\s*\[(.*?)\]", packaging_tf, re.M | re.S)
        assert match, f"{name} is gone from infra-terraform/packaging.tf"
        return re.findall(r'"([^"]+)"', match.group(1))

    root_files, trees = tf_list("layer_root_files"), tf_list("layer_trees")
    missing = [
        path
        for path in read_by_build
        if path not in root_files
        and not any(path == tree or path.startswith(f"{tree}/") for tree in trees)
    ]
    assert not missing, (
        f"the layer archive in infra-terraform/packaging.tf omits {missing}, which "
        "`pip install .` reads; add them to layer_root_files or layer_trees"
    )


@pytest.mark.slow
def test_pip_installed_cli_scaffolds_and_lists(installed_cli: Path, tmp_path: Path):
    """A pip-installed socialagent can init a project and list recipes.

    This is the external user's first five minutes. It runs the real console
    script in a venv with no repo root above it, so it fails if the CLI reaches
    outside its own package for data.
    """
    socialagent = installed_cli / "socialagent"

    work = tmp_path / "work"
    work.mkdir()

    init = subprocess.run(
        [str(socialagent), "init", "mybot", "--blueprint", "minimal"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, (
        f"socialagent init failed:\n{init.stdout}\n{init.stderr}"
    )

    project = work / "mybot"
    assert (project / "agent.py").is_file()
    # The vendored copy is what lets the scaffolded project import socialagent_runtime.
    assert (project / "socialagent_runtime" / "notify.py").is_file()

    recipes = subprocess.run(
        [str(socialagent), "recipe", "list"], capture_output=True, text=True
    )
    assert recipes.returncode == 0, f"socialagent recipe list failed:\n{recipes.stderr}"
    assert "rich-messages" in recipes.stdout
    # A missing recipes dir degrades to this placeholder instead of erroring.
    assert "No description available" not in recipes.stdout

    add = subprocess.run(
        [str(socialagent), "recipe", "add", "rich-messages"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert add.returncode == 0, f"socialagent recipe add failed:\n{add.stderr}"
    assert (project / "src" / "tools" / "rich_messages.py").is_file()


@pytest.mark.slow
def test_pip_installed_init_instructions_match_scaffold(
    installed_cli: Path, tmp_path: Path
):
    """The printed next-steps must work for the project that was created.

    Scaffolded projects ship requirements.txt and no pyproject.toml, so telling
    the user to run `uv sync` sends them into an error on their first command.
    """
    work = tmp_path / "work"
    work.mkdir()
    init = subprocess.run(
        [str(installed_cli / "socialagent"), "init", "mybot", "--blueprint", "minimal"],
        cwd=work,
        capture_output=True,
        text=True,
    )

    project = work / "mybot"
    assert (project / "requirements.txt").is_file()
    assert not (project / "pyproject.toml").exists()
    assert "uv sync" not in init.stdout, (
        "init suggests `uv sync` but the scaffold has no pyproject.toml"
    )
    assert "requirements.txt" in init.stdout


# ── Does the scaffolded project actually run? ─────────────────────────────────


def _make_runtime_venv(at: Path) -> Path:
    """Create a venv with no ``socialagent`` installed; return its bin directory.

    Deliberately bare, and deliberately one per blueprint rather than shared:
    the AgentCore container installs only that project's ``requirements.txt``
    (see each blueprint's Dockerfile), never the SDK. A shared venv would let a
    blueprint with an incomplete requirements.txt boot on packages some earlier
    blueprint installed — which is exactly the bug this is meant to catch, since
    all three currently declare identical dependencies.

    Booting without ``socialagent`` also proves the scaffold does not quietly depend on
    the SDK, which would work in a checkout and fail in the container.
    """
    subprocess.run(
        [sys.executable, "-m", "venv", str(at)], check=True, capture_output=True
    )
    return _venv_bin(at)


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_until_serving(proc: subprocess.Popen, blueprint: str) -> None:
    """Poll /ping until the agent answers, or fail saying why it never did."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(
                f"'{blueprint}' agent.py exited with code {proc.returncode} instead of "
                f"serving {_HEALTH_URL}:\n{(err or out)[-3000:]}"
            )
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.5)

    _terminate(proc)
    out, err = proc.communicate()
    pytest.fail(
        f"'{blueprint}' agent.py never answered {_HEALTH_URL} within "
        f"{_BOOT_TIMEOUT_S}s:\n{(err or out)[-3000:]}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("blueprint", sorted(_BLUEPRINT_NAMES))
def test_scaffolded_project_boots(installed_cli: Path, tmp_path: Path, blueprint: str):
    """Every blueprint scaffolds into a project that starts and serves /ping.

    Scaffolding files is not the same as scaffolding something that runs. A
    dependency that is missing or mispinned, a broken import under the generated
    ``src/``, or a module-level model construction that needs a region all leave
    ``socialagent init`` green and fail on the user's first real run — the same shape as
    the wheel bug these tests exist for, one step later.

    Boots rather than imports because every blueprint calls ``app.run()`` at
    module level, so importing ``agent.py`` never returns. Readiness is the
    ``/ping`` endpoint each Dockerfile health-checks.
    """
    # No port override exists, so these run one at a time. Under -p xdist they
    # would collide; skipping beats a confusing bind error.
    if _port_in_use(_AGENT_PORT):
        pytest.skip(f"port {_AGENT_PORT} is in use — the agent server needs it")

    work = tmp_path / "work"
    work.mkdir()
    init = subprocess.run(
        [str(installed_cli / "socialagent"), "init", "bot", "--blueprint", blueprint],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, (
        f"socialagent init --blueprint {blueprint} failed:\n{init.stdout}\n{init.stderr}"
    )

    project = work / "bot"
    requirements = project / "requirements.txt"
    assert requirements.is_file(), f"'{blueprint}' scaffolded no requirements.txt"

    runtime_venv = _make_runtime_venv(tmp_path / "runtime")
    deps = subprocess.run(
        [str(runtime_venv / "pip"), "install", "-q", "-r", str(requirements)],
        capture_output=True,
        text=True,
    )
    assert deps.returncode == 0, (
        f"'{blueprint}' ships a requirements.txt that does not install:\n"
        f"{deps.stderr[-3000:]}"
    )

    # Same sentinels `socialagent dev` sets, so socialagent_runtime's dev stubs activate instead
    # of reaching for AWS. The region and dummy credentials are required, not
    # cosmetic: blueprints that build a BedrockModel at module level construct a
    # boto3 client on import, and botocore refuses without a region.
    env = {
        **os.environ,
        **_DEV_ENV,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }
    proc = subprocess.Popen(
        [str(runtime_venv / "python"), "agent.py"],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_serving(proc, blueprint)
    finally:
        if proc.poll() is None:
            _terminate(proc)
