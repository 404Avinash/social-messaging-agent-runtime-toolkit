---
id: ADR-0012
title: "The CLI ships and manages the stack's Terraform, so a pip install covers the whole lifecycle"
status: Proposed
date: 2026-11-10
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, deploy, terraform, cli]
---

# ADR-0012: The CLI ships and manages the stack's Terraform, so a pip install covers the whole lifecycle

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-10 | SMART maintainers |

## Context

The toolkit installs from PyPI as `smart-socialagent` ([ADR-0008](0008-pypi-distribution-name.md)), but only half of its lifecycle works from that install. `init`, `recipe`, `dev`, `config init/show`, `templates`, `secrets` and `status` run from the wheel; everything that runs Terraform does not. `find_tf_dir` in `src/socialagent/cli/_deploy.py` looks for an `infra-terraform/` checkout, which the wheel does not ship, and the Terraform itself reaches outside its directory: `infra-terraform/packaging.tf` zips the SDK from `${path.root}/..` and runs `pip install .` on it to build the Lambda layer, and the `templates` module call in `infra-terraform/main.tf` bundles `src/socialagent` and `pyproject.toml` the same way for template sync. The header of `packaging.tf` calls this the fork model. The Terraform also writes its build archives under `${path.root}/.terraform/` and `${path.module}/.terraform/`, so it cannot run from a shared or read-only `site-packages`. The agent image is not part of the problem: it builds from the project directory alone, from files `init` writes out of the wheel, including the vendored runtime ([ADR-0003](0003-agent-runtime-is-vendored.md)).

Whatever replaces the clone must keep three things true: an existing stack deploys from the new layout without moving state (the AgentCore Runtime must never be replaced, see `AGENTS.md`), a contributor working from a checkout keeps today's behaviour, and the hash of what the layer is built from stays the only thing that triggers a layer rebuild.

## Decision

Recommended: the wheel carries the stack's Terraform root module, and the CLI materializes it per project under `.socialagent/infra/<version>/`, replacing it when its content changes; outside a checkout the Lambda layer and template sync install the SDK from a package spec that defaults to `smart-socialagent==<CLI version>` instead of `pip install .`; a project grants its tools extra IAM through two variables instead of editing the module; `socialagent tf` runs any other Terraform command with the stack's context; and `socialagent infra eject` copies the module into the project for anyone who wants to own it. The module stays one root with the same resource addresses, so no state moves. The full proposal, the inventory and the pull request plan are in issue #133.

Question for the maintainers: accept CLI-managed Terraform as the default, with project-owned Terraform as an explicit `eject`, rather than scaffolding the Terraform into every project?

## Alternatives considered

- **Scaffold the Terraform into every project at `init`.** The project owns it from day one and can edit IAM freely. Why not as the default: every upgrade becomes a hand merge of a large module into every project, and most projects never change it. `infra eject` gives the same result to the projects that need it.
- **Publish the module from a tag (`source = "github.com/...//infra-terraform?ref=v<version>"`) and give each project a small root.** Clean upgrades by bumping a ref. Why not now: the stack's root would become a child module, so every resource address moves and every existing stack needs `moved` blocks or a state migration, with the AgentCore Runtime among them. Worth revisiting once the root is small enough to split.
- **Build the layer from the installed distribution's files (`importlib.metadata`).** Keeps the content-hash trigger and needs no index during the deploy. Why not as the default: the layer's dependencies would come from the deploying machine's resolution rather than CodeBuild's, and the files of an editable or partial install are not a reliable source. The package spec can point at a wheel in S3 for an unreleased build.
- **Ship the runtime as a pip dependency at the same time (#132).** Removes the vendored copy and its staleness check. Not needed for this goal, since the image already builds without the repository, and it reverses ADR-0003, so it stays a separate decision.

## Consequences

- `pip install smart-socialagent` is enough to `bootstrap`, `deploy`, `destroy` and `status --refresh` a stack; the README and the getting-started guide lead with it, and the clone becomes the contributor path.
- `find_tf_dir` gains an order: `--tf-dir`, then `terraform.dir` in the YAML, then a checkout, then the managed copy. `config_cmd` uses the same resolver instead of its own copy.
- `packaging.tf` and the `templates` module get two modes, chosen by whether `pyproject.toml` exists at `${path.root}/..`. In package mode the spec, plus the wheel's MD5 when it is a file, drives the S3 key, the build trigger and `source_code_hash`; no `terraform_data` trigger is added beside it. Package mode installs from PyPI in CodeBuild, a new source for the layer, so it gets a security review before merge.
- The IAM extension variables attach statements only to the two existing custom policies; the hardening tests that keep `social-messaging:` off the runtime role still apply. They change what IAM can grant, so they get a security review too.
- The managed copy is replaced by hash, so editing it is pointless; the CLI says so, and `infra eject` is the way to own the Terraform. An ejected copy is never overwritten, and the CLI warns when it is older than the CLI.
- `socialagent tf` puts `apply` and `destroy` through the same checks and confirmation as `deploy` and `destroy`, and refuses `-replace`, `taint` and `state rm` on the AgentCore Runtime without `--force`.
- Planned, not shipped: deploying the admin console without a clone, and the runtime lineage without git (#127), which this record does not depend on.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Is installing the layer's SDK from PyPI in CodeBuild acceptable, or should package mode always build from a wheel the CLI uploads?
- Should the managed copy live per project (`.socialagent/infra/<version>/`) or once per user, with only the data directories per project?
- Does `socialagent tf` belong in the first release of this change, or after the basic lifecycle works?

## Enforced by

Nothing yet. A Proposed record enforces nothing; the pull requests in #133 add the tests that will be listed here when it is accepted.
