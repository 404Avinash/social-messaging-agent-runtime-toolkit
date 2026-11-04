# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``socialagent secrets set`` — put a secret's value straight into Secrets Manager.

``socialagent.<env>.yaml`` lists secret *names* only
(``secrets: [partner_api]``); Terraform creates each secret, seeds it with
``{}`` and then ignores its value (``ignore_changes``). The value is set here,
so it is never written to the project, a generated tfvars, shell history, or
— after creation — Terraform state.

The secret is addressed exactly as Terraform names it
(``infra-terraform/modules/secrets``): ``<environment>-<project>-<name>``.
The value is a JSON object of key → string, like the Terraform variable:

- ``--key api_key --key api_secret`` prompts for each value without echo and
  updates only those keys: the secret's other keys are kept;
- ``--from-file <path>`` (``-`` for stdin) replaces the whole object. The file
  must live outside the project: the agent-source archive ships everything in
  the project directory, so a credentials file there would be uploaded to S3.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from socialagent.cli._errors import CliError
from socialagent.cli._project import project_root
from socialagent.cli.config_file import check_environment, find_config, load


def secret_id(config: dict[str, Any], name: str) -> str:
    """The Secrets Manager name Terraform gives secret *name*."""
    return f"{config['environment']}-{config['project']}-{name}"


def _read_value(args: argparse.Namespace, project_dir: Path) -> dict[str, str]:
    if args.from_file and args.key:
        raise CliError("use either --from-file or --key, not both")
    if args.from_file == "-":
        value = _parse(sys.stdin.read(), "stdin")
    elif args.from_file:
        path = Path(args.from_file)
        if path.resolve().is_relative_to(project_dir.resolve()):
            raise CliError(
                f"{path} is inside the project, and the next deploy uploads the "
                "project directory to S3 with it. Keep credential files outside the "
                "project, or pipe the JSON in with --from-file -"
            )
        try:
            value = _parse(path.read_text(), str(path))
        except OSError as exc:
            raise CliError(f"cannot read {path}: {exc.strerror}") from exc
    elif args.key:
        value = {k: getpass.getpass(f"{k}: ") for k in args.key}
    else:
        raise CliError("give the value with --key <name> (prompted) or --from-file")
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise CliError(
            'the value must be a JSON object of strings, e.g. {"api_key": "..."}'
        )
    if not value:
        raise CliError("the value is empty")
    blank = sorted(k for k, v in value.items() if not v.strip())
    if blank:
        raise CliError(f"empty value for {', '.join(blank)} — nothing was written")
    return value


def _parse(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"{source} is not valid JSON: {exc}") from exc


def _current_value(client: Any, sid: str) -> dict[str, str]:
    """The secret's current object, so ``--key`` can update keys in place."""
    raw = client.get_secret_value(SecretId=sid)["SecretString"]
    try:
        current = json.loads(raw)
    except json.JSONDecodeError:
        current = None
    if not isinstance(current, dict):
        raise CliError(
            f"{sid} does not hold a JSON object, so --key cannot update it in "
            "place. Replace the whole value with --from-file"
        )
    return current


def cmd_secrets_set(args: argparse.Namespace, client: Any = None) -> None:
    """Set the value of a secret listed in ``socialagent.<env>.yaml``."""
    root = project_root()
    cfg_path = find_config(root, args.env)
    if not cfg_path.exists():
        raise CliError(f"config not found: {cfg_path}")
    config = load(cfg_path)
    check_environment(config, args.env, cfg_path)
    declared = config.get("secrets") or []
    if args.name not in declared:
        listed = ", ".join(declared) or "none"
        raise CliError(
            f"'{args.name}' is not listed under `secrets:` in {cfg_path.name} "
            f"(listed: {listed}). Add it there and deploy, so Terraform creates "
            "the secret and grants read access to the runtime and to the webhook, "
            "event and scheduler Lambdas."
        )
    value = _read_value(args, root)

    if client is None:
        import boto3  # noqa: PLC0415

        client = boto3.client("secretsmanager", region_name=config["region"])
    sid = secret_id(config, args.name)
    kept: list[str] = []
    try:
        if args.key:
            current = _current_value(client, sid)
            kept = sorted(set(current) - set(value))
            value = {**current, **value}
        client.put_secret_value(SecretId=sid, SecretString=json.dumps(value))
    except client.exceptions.ResourceNotFoundException as exc:
        raise CliError(
            f"secret {sid} does not exist in {config['region']} — deploy the stack "
            "first so Terraform creates it"
        ) from exc
    written = sorted(set(value) - set(kept))
    line = f"✓ Set {sid}: {', '.join(written)}"
    if kept:
        line += f" (kept {', '.join(kept)})"
    print(line)
