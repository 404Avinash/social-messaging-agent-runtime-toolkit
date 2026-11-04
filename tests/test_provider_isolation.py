# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static-analysis tests enforcing provider isolation and ``socialagent_runtime`` purity.

These are deterministic source-scanning tests (no hypothesis required). They guard
two architectural invariants of the SMART layout:

* ``boto3.client("socialmessaging")`` only ever appears in the
  Lambda Control Layer provider (``src/socialagent/providers/eum_social.py``); never in
  agent-side code under ``packages/``, ``projects/``, or ``recipes/``.
* The Agent Runtime Package (``packages/socialagent-runtime/``) never imports
  from the Lambda Control Layer namespaces ``socialagent.capabilities``, ``socialagent.skills``,
  or ``socialagent.tools``.
"""

from pathlib import Path


def test_no_direct_socialmessaging_calls():
    # No socialmessaging client outside eum_social.py.
    repo_root = Path(__file__).resolve().parents[1]
    needle = 'boto3.client("socialmessaging"'
    offenders = []
    for base in ("packages", "projects", "recipes"):
        base_dir = repo_root / base
        if not base_dir.is_dir():
            continue
        for py in base_dir.rglob("*.py"):
            # Skip virtualenv / cache noise if it ever appears under these dirs.
            parts = py.parts
            if ".venv" in parts or "__pycache__" in parts:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                offenders.append(str(py.relative_to(repo_root)))
    assert offenders == [], f"Direct socialmessaging calls found in: {offenders}"


def test_socialagent_runtime_no_forbidden_imports():
    # socialagent_runtime imports no forbidden namespaces.
    repo_root = Path(__file__).resolve().parents[1]
    pkg = repo_root / "packages" / "socialagent-runtime"
    forbidden = ("socialagent.capabilities", "socialagent.skills", "socialagent.tools")
    offenders = []
    for py in pkg.rglob("*.py"):
        parts = py.parts
        if ".venv" in parts or "__pycache__" in parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        for ns in forbidden:
            # Match both `import <ns>` and `from <ns> import ...` forms.
            if f"import {ns}" in text or f"from {ns}" in text:
                offenders.append((str(py.relative_to(repo_root)), ns))
    assert offenders == [], f"Forbidden imports found: {offenders}"
