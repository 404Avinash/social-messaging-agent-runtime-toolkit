---
id: ADR-0012
title: "The package carries the stack's Terraform, so a pip install covers the whole lifecycle"
status: Proposed
date: 2026-11-11
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, deploy, terraform, cli]
---

# ADR-0012: The package carries the stack's Terraform, so a pip install covers the whole lifecycle

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-11 | SMART maintainers |

## Context

The toolkit installs from PyPI as `smart-socialagent` ([ADR-0008](0008-pypi-distribution-name.md)), but only half of its lifecycle works from that install. `init`, `recipe`, `dev`, `config init/show`, `templates`, `secrets` and `status` run from the wheel; everything that runs Terraform does not. `find_tf_dir` in `src/socialagent/cli/_deploy.py` looks for an `infra-terraform/` checkout, which the wheel does not ship, and the Terraform itself reaches outside its directory: `infra-terraform/packaging.tf` zips the SDK from `${path.root}/..` and runs `pip install .` on it to build the Lambda layer, and the `templates` module call in `infra-terraform/main.tf` bundles `src/socialagent` and `pyproject.toml` the same way for template sync. The header of `packaging.tf` calls this the fork model. The Terraform also writes its build archives under `${path.root}/.terraform/` and `${path.module}/.terraform/`, so it cannot run from a shared or read-only `site-packages`.

The agent runtime is a separate question. `socialagent init` copies it into every project as `socialagent_runtime/` ([ADR-0003](0003-agent-runtime-is-vendored.md)), from the copy the wheel already carries as data (`socialagent/cli/_assets/socialagent-runtime`), so the vendored runtime does not need a clone. It does have costs of its own: the copy drifts from the CLI that deployed it, `socialagent runtime status` can only report the drift, and a dependency scanner or Dependabot in the project's repository does not see it as a versioned package. This record leaves that question open and says only which direction it should take (see Alternatives considered).

Whatever replaces the clone must keep four things true: an existing stack deploys from the new layout without moving state (the AgentCore Runtime must never be replaced, see `AGENTS.md`), a contributor working from a checkout keeps today's behaviour, the hash of what the layer is built from stays the only thing that triggers a layer rebuild, and the Lambdas and the agent keep checking the payload contract (`SOCIALAGENT_VERSION`) because they are built at different moments.

## Decision

Recommended: **the Terraform comes from the package.** The wheel carries the stack's Terraform root module, and the CLI materializes it per project under `.socialagent/infra/<version>/`, replacing it when its content changes. `find_tf_dir` resolves, in order, `--tf-dir`, `terraform.dir` in the YAML, a checkout, then the managed copy. Outside a checkout the Lambda layer and template sync install the SDK from a package spec that defaults to `smart-socialagent==<CLI version>` instead of `pip install .`, and `--sdk-wheel` points it at a local wheel for an unreleased build. The module stays one root with the same resource addresses, so no state moves.

The agent runtime stays vendored, and ADR-0003 stands. With this record, `pip install smart-socialagent` is enough to `init`, `bootstrap`, `deploy`, `destroy` and `status --refresh` a stack.

Question for the maintainers: accept CLI-managed Terraform as the default for a project created from the package, with the checkout as the contributor path?

## Alternatives considered

- **Scaffold the Terraform into every project at `init`.** The project owns it from day one and can edit IAM freely. Why not as the default: every upgrade becomes a hand merge of a large module into every project, and most projects never change it.
- **Publish the module from a tag (`source = "github.com/...//infra-terraform?ref=v<version>"`) and give each project a small root.** Clean upgrades by bumping a ref. Why not now: the stack's root would become a child module, so every resource address moves and every existing stack needs `moved` blocks or a state migration, with the AgentCore Runtime among them. Worth revisiting once the root is small enough to split.
- **Build the layer from the installed distribution's files (`importlib.metadata`).** Keeps the content-hash trigger and needs no index during the deploy. Why not as the default: the layer's dependencies would come from the deploying machine's resolution rather than CodeBuild's, and the files of an editable or partial install are not a reliable source. The package spec can point at a wheel for an unreleased build.
- **Ship the runtime inside `smart-socialagent`, behind a `runtime` extra, and pin `smart-socialagent[runtime]==<version>` in each project.** One distribution and one version. Why not: the agent image would carry the whole SDK and its dependencies, about 9 MB measured against the runtime's 0.16 MB, most of it `pygments` and `rich`, which only the CLI uses. Every SDK dependency floor would then constrain each project's agent, every release would rebuild every agent whether or not the runtime changed, and an image scan would report findings in code the agent never imports. A Python extra only adds dependencies, so no variant of one distribution keeps that code out of the image.
- **Publish the runtime as its own distribution, `smart-socialagent-runtime`, the name ADR-0008 reserves for it, with `smart-socialagent` depending on it.** A developer still installs one package, the agent image gets only the runtime, and the project pins a versioned package that scanners and Dependabot see. Both distributions read the same `VERSION`, publish from the same release job with the runtime first, and pin each other with `==`, so no CLI on the index ever pins a runtime that is not there yet. Why not in this record: it needs a second PyPI project, a release that publishes two distributions with a check that their versions match, and the migration of existing projects off the vendored copy. It is the recommended direction for the runtime, to be decided in its own record that supersedes ADR-0003.

## Consequences

- The README and the getting-started guide lead with `pip install smart-socialagent`, and the clone becomes the contributor path.
- `config_cmd` uses the same Terraform resolver as `deploy` instead of its own copy, and `deploy` checks that `terraform` and `aws` are on `PATH`.
- `packaging.tf` and the `templates` module get two modes, chosen by whether `pyproject.toml` exists at `${path.root}/..`. In package mode the spec, plus the wheel's MD5 when it is a file, drives the S3 key, the build trigger and `source_code_hash`; no `terraform_data` trigger is added beside it. Package mode installs from PyPI in CodeBuild, a new source for the layer, so it gets a security review before merge.
- The managed copy is replaced by hash, so editing it is pointless, and the CLI says so. A project that needs project-specific IAM still edits `infra-terraform/modules/iam/main.tf` in a checkout, as `AGENTS.md` describes.
- A layer built in package mode needs the pinned release on PyPI, so a release publishes before any deploy pins it, and a contributor tests an unreleased build with `--sdk-wheel`.
- `SOCIALAGENT_VERSION` stays the guard between the Lambdas and the agent.
- Every stack rebuilds its Lambda layer once, on the first deploy in package mode, because what the layer is built from changes.
- Planned, not shipped, each with its own issue:
  - Two variables that attach project statements to the existing custom policies, and `socialagent infra eject`, which copies the module into the project for anyone who wants to own it. The variables come with an explicit deny on `social-messaging:*` on the runtime role that they cannot override, and a validation that rejects any `social-messaging:` action at plan time, each with a test; both get a security review.
  - `socialagent tf`: any other Terraform command with the stack's context, with the same checks as `deploy` and `destroy`, refusing `-replace`, `taint` and `state rm` on the AgentCore Runtime without `--force`.
  - Deploying the admin console without a clone.
  - The runtime as `smart-socialagent-runtime` (see Alternatives considered), including the release check that the two distributions publish together with the same version.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Is installing the layer's SDK from PyPI in CodeBuild acceptable, or should package mode always build from a wheel the CLI uploads? Recommended: the PyPI spec pinned to the exact version, with a local wheel as the override for unreleased builds.
- Should the managed copy live per project (`.socialagent/infra/<version>/`) or once per user, with only the data directories per project? Recommended: per project, because Terraform writes its build archives inside the module and two projects must not share them.

## Enforced by

Nothing yet. A Proposed record enforces nothing; the pull requests that implement it (#165 and #167) add the tests that will be listed here when it is accepted.
