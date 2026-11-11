---
id: ADR-0012
title: "The package carries the stack's Terraform and the agent runtime, so a pip install covers the whole lifecycle"
status: Proposed
date: 2026-11-11
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, deploy, terraform, cli, runtime]
---

# ADR-0012: The package carries the stack's Terraform and the agent runtime, so a pip install covers the whole lifecycle

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-11 | SMART maintainers |

## Context

The toolkit installs from PyPI as `smart-socialagent` ([ADR-0008](0008-pypi-distribution-name.md)), but only half of its lifecycle works from that install. `init`, `recipe`, `dev`, `config init/show`, `templates`, `secrets` and `status` run from the wheel; everything that runs Terraform does not. `find_tf_dir` in `src/socialagent/cli/_deploy.py` looks for an `infra-terraform/` checkout, which the wheel does not ship, and the Terraform itself reaches outside its directory: `infra-terraform/packaging.tf` zips the SDK from `${path.root}/..` and runs `pip install .` on it to build the Lambda layer, and the `templates` module call in `infra-terraform/main.tf` bundles `src/socialagent` and `pyproject.toml` the same way for template sync. The header of `packaging.tf` calls this the fork model. The Terraform also writes its build archives under `${path.root}/.terraform/` and `${path.module}/.terraform/`, so it cannot run from a shared or read-only `site-packages`.

The project is not clean either. `socialagent init` copies the agent runtime into every project as `socialagent_runtime/` ([ADR-0003](0003-agent-runtime-is-vendored.md)), each blueprint's Dockerfile copies that directory into the image, and the zip-mode build ships it with the agent's code. A copy drifts from the CLI that deployed it, and `socialagent runtime status` can only report the drift, not prevent it. ADR-0003 named a pip dependency as "the clean answer once the toolkit is published", which it now is. The wheel already carries the runtime's source, as data for `init` to copy (`socialagent/cli/_assets/socialagent-runtime`), so making it importable from the same wheel costs a mapping, not a second distribution.

[ADR-0001](0001-two-runtime-layers.md) rejected "one package installed in both containers" because the agent image would carry the Lambda dependencies and nothing would stop a tool from building a WhatsApp client. Both reasons are weaker than they were. The Lambda layer's own dependencies are `boto3`, which the agent image already has, plus `cryptography`, `pyyaml` and `rich`. What stops a tool from calling WhatsApp is the AgentCore Runtime role, which grants no `social-messaging:` action (`tests/test_terraform_hardening.py::test_runtime_role_has_no_social_messaging_actions`, and, with this record, an explicit deny no project variable can override): with `boto3` in the image, any tool can already build the client, so the package boundary never was the control. The import rule, which is what keeps the layers apart, does not depend on how they ship.

Whatever replaces the clone and the copy must keep four things true: an existing stack deploys from the new layout without moving state (the AgentCore Runtime must never be replaced, see `AGENTS.md`), a contributor working from a checkout keeps today's behaviour, the hash of what the layer is built from stays the only thing that triggers a layer rebuild, and the Lambdas and the agent keep checking the payload contract (`SOCIALAGENT_VERSION`) because they are built at different moments.

## Decision

Recommended, in two parts that ship in the same release.

**The Terraform comes from the package.** The wheel carries the stack's Terraform root module, and the CLI materializes it per project under `.socialagent/infra/<version>/`, replacing it when its content changes. `find_tf_dir` resolves, in order, `--tf-dir`, `terraform.dir` in the YAML, a checkout, then the managed copy. Outside a checkout the Lambda layer and template sync install the SDK from a package spec that defaults to `smart-socialagent==<CLI version>` instead of `pip install .`. A project grants its tools extra IAM through two variables instead of editing the module (the runtime role keeps an explicit deny on `social-messaging:*` that those variables cannot override, see Consequences), and `socialagent infra eject` copies the module into the project for anyone who wants to own it. The module stays one root with the same resource addresses, so no state moves.

**The runtime comes from the same package.** The `smart-socialagent` wheel also installs the agent runtime as the top-level package `socialagent_runtime`, built from `packages/socialagent-runtime/`, where it stays. The import name does not change, so no blueprint, recipe or project changes an import. `socialagent init` stops copying the runtime and writes `smart-socialagent[runtime]==<CLI version>` into the project's `requirements.txt` (the `runtime` extra adds `bedrock-agentcore`, the runtime's one dependency the CLI does not need), and the blueprints' Dockerfiles stop copying `socialagent_runtime/`. There is one distribution and one version: the CLI, the layer and the agent of a stack built by one CLI come from the same release. `socialagent runtime update` becomes the migration of an existing project: it backs up and removes the copied `socialagent_runtime/`, pins the package, and removes the copy from the Dockerfile; a copy with content no CLI shipped still needs `--force`, as today. `socialagent runtime status` compares the project's pin with the CLI and reports a leftover copy. The two layers still never import each other (ADR-0001's rule and its tests stand); only the packaging alternative ADR-0001 rejected is revisited. This supersedes ADR-0003, and amends ADR-0008, which planned the runtime as a second distribution named `smart-socialagent-runtime`; that name stays the one to use if the runtime is ever published on its own.

Question for the maintainers: accept CLI-managed Terraform as the default, with project-owned Terraform as an explicit `eject`, and the runtime as part of the same package rather than a second distribution, which reverses the packaging alternative ADR-0001 rejected?

## Alternatives considered

- **Scaffold the Terraform into every project at `init`.** The project owns it from day one and can edit IAM freely. Why not as the default: every upgrade becomes a hand merge of a large module into every project, and most projects never change it. `infra eject` gives the same result to the projects that need it.
- **Publish the module from a tag (`source = "github.com/...//infra-terraform?ref=v<version>"`) and give each project a small root.** Clean upgrades by bumping a ref. Why not now: the stack's root would become a child module, so every resource address moves and every existing stack needs `moved` blocks or a state migration, with the AgentCore Runtime among them. Worth revisiting once the root is small enough to split.
- **Build the layer from the installed distribution's files (`importlib.metadata`).** Keeps the content-hash trigger and needs no index during the deploy. Why not as the default: the layer's dependencies would come from the deploying machine's resolution rather than CodeBuild's, and the files of an editable or partial install are not a reliable source. The package spec can point at a wheel in S3 for an unreleased build.
- **Publish the runtime as a second distribution, `smart-socialagent-runtime`.** The agent image would carry only the runtime and its two dependencies, `boto3` and `bedrock-agentcore`; the CLI already needs `boto3`, so the `runtime` extra adds only `bedrock-agentcore` (and the `strands-agents<2` pin every blueprint carries). Why not now: a second PyPI project, trusted publisher and release step, and two versions that must move together. The agent image gains the CLI and the Lambda code it never imports (under 1 MB) and the CLI's dependencies (`cryptography`, `pyyaml`, `rich`; `boto3` it already has). The layering rules do not depend on the split: `tests/test_architecture.py` keeps the two layers from importing each other whatever ships them.
- **Keep the vendored copy.** No migration for existing projects. Why not: the copy is the thing that drifts, and the project carries toolkit code it should not edit.

## Consequences

- `pip install smart-socialagent` is enough to `init`, `bootstrap`, `deploy`, `destroy` and `status --refresh` a stack, and a new project holds only its own code and configuration. The README and the getting-started guide lead with it, and the clone becomes the contributor path.
- `config_cmd` uses the same Terraform resolver as `deploy` instead of its own copy, and `deploy` checks that `terraform` and `aws` are on `PATH`.
- `packaging.tf` and the `templates` module get two modes, chosen by whether `pyproject.toml` exists at `${path.root}/..`. In package mode the spec, plus the wheel's MD5 when it is a file, drives the S3 key, the build trigger and `source_code_hash`; no `terraform_data` trigger is added beside it. Package mode installs from PyPI in CodeBuild, a new source for the layer, so it gets a security review before merge.
- The IAM extension variables attach statements only to the two existing custom policies, and they cannot reopen what this record calls the real control. The runtime role gets a policy the variables never touch that explicitly denies `social-messaging:*`, so a project statement granting `social-messaging:SendWhatsAppMessage`, `social-messaging:*` or `*` still has no effect (an explicit deny wins over any allow). The runtime variable's `validation` also rejects any `social-messaging:` action, so the mistake fails at plan time with a clear message. The text check in `tests/test_terraform_hardening.py` sees only the module, not what a project passes in, so the implementation adds a test that the deny exists and is attached, and a test that the validation rejects such a statement; both go in Enforced by. The variables change what IAM can grant, so they get a security review too.
- The managed copy is replaced by hash, so editing it is pointless; the CLI says so, and `infra eject` is the way to own the Terraform. An ejected copy is never overwritten, and the CLI warns when it is older than the CLI.
- An agent image or zip package needs the pinned release on PyPI before it builds, so a release publishes before any project pins it, and a contributor tests an unreleased build from a local wheel. The zip build excludes a leftover `socialagent_runtime/` from the project, because its `rsync` would otherwise copy it over the installed package; in a Docker image the project's directory comes first on `sys.path`, which `runtime status` and `deploy` report.
- The zip build deletes `*.dist-info`, so the runtime never reads its own version from package metadata; the version is a constant generated at build time.
- `SOCIALAGENT_VERSION` stays the guard between the Lambdas and the agent, and its error message points at `runtime update` instead of a manual copy.
- **Acceptance updates two records at once.** While this record is Proposed it replaces nothing, so its `supersedes` stays empty. The change that accepts it sets this record to `Accepted` with `supersedes: [ADR-0003]` and a real Enforced-by list, sets ADR-0003 to `Superseded by ADR-0012` (front matter and table), and updates both rows of the `docs/adr/README.md` index, in the same commit as the code that stops copying the runtime.
- Every stack rebuilds its Lambda layer once, on the first deploy with this change, because what the layer is built from changes.
- Planned, not shipped: `socialagent tf` (any other Terraform command with the stack's context, with the same checks as `deploy` and `destroy`, refusing `-replace`, `taint` and `state rm` on the AgentCore Runtime without `--force`), and deploying the admin console without a clone. The runtime lineage without git (#127) is needed only to classify a copy during migration from a wheel install.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Is installing the layer's SDK from PyPI in CodeBuild acceptable, or should package mode always build from a wheel the CLI uploads? Recommended: the PyPI spec pinned to the exact version, with an S3 wheel as the override for unreleased builds.
- Should the managed copy live per project (`.socialagent/infra/<version>/`) or once per user, with only the data directories per project? Recommended: per project, because Terraform writes its build archives inside the module and two projects must not share them.
- Should `runtime update` also edit a Dockerfile the project changed, or only report what to remove? Recommended: edit only the exact line the blueprints ship, and report anything else.

## Enforced by

Nothing yet. A Proposed record enforces nothing; the pull requests in #133 and #132 add the tests that will be listed here when it is accepted.
