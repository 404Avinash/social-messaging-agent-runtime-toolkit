# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The registry entry Terraform writes carries every field the console writes.

Two writers share each stack's ``socialagent.yaml`` in the console registry:
``aws_s3_object.admin_registry`` in ``infra-terraform/main.tf`` and the
console's phone-number bind. The bind also writes ``meta_phone_number_id`` and
``shared_sns_topic_arn``, and reads ``shared_sns_topic_arn`` back. Terraform
omitted both, so any deploy that rewrote the entry erased them, whatever the
project config said.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_TF = REPO_ROOT / "infra-terraform" / "main.tf"
PHONE_ROUTES = (
    REPO_ROOT / "socialagent-admin-console" / "backend" / "routes" / "phone_numbers.py"
)


def _registry_yaml() -> str:
    """The ``yamlencode({...})`` body of ``local.socialagent_yaml``."""
    tf = MAIN_TF.read_text()
    start = tf.index("socialagent_yaml = yamlencode({")
    return tf[start : tf.index("\n  })\n", start)]


def _top_level_keys(body: str) -> dict[str, str]:
    """Keys at the first nesting level of the yamlencode map, with their value."""
    return dict(re.findall(r"^    (\w+)\s*=\s*(.+)$", body, flags=re.MULTILINE))


def test_registry_publishes_the_shared_waba_fields() -> None:
    keys = _top_level_keys(_registry_yaml())

    assert keys.get("meta_phone_number_id") == "var.meta_phone_number_id"
    assert keys.get("shared_sns_topic_arn") == "var.shared_sns_topic_arn"
    assert keys.get("phone_number_id") == "var.phone_number_id"


def test_every_field_the_console_bind_writes_comes_from_terraform_too() -> None:
    """A key only the console writes is erased by the next rewrite of the entry.

    The bind holds the target stack's entry in ``config`` and a sibling's in
    ``scfg``.
    """
    written = set(
        re.findall(r'\b(?:config|scfg)\["(\w+)"\]\s*=(?!=)', PHONE_ROUTES.read_text())
    )
    assert written, "found no registry writes in the phone-number routes"

    missing = written - set(_top_level_keys(_registry_yaml()))
    assert not missing, (
        f"the console writes {sorted(missing)}, which local.socialagent_yaml omits"
    )
