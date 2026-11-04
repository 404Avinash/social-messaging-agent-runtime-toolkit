---
id: ADR-0007
title: "Templates are provider-agnostic YAML; the provider implements the sync"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [templates, providers]
---

# ADR-0007: Templates are provider-agnostic YAML; the provider implements the sync

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

WhatsApp lets a business message a user outside the 24-hour window only with a pre-approved template. Templates are therefore part of a project's code: a reminder handler cannot run without the `reminder` template registered and approved, and a change to its wording is a change to behaviour. Meta approves templates through the provider's API, in the toolkit's case AWS End User Messaging Social, and rejects or reclassifies them for reasons it publishes only partly: a variable at either end of the body, too many variables for the text, emoji or branding in a UTILITY template.

Registering templates by hand in a console does not survive a second environment or a second developer, and a project that registers them through provider API calls in its own code ties its templates to one provider and scatters the API across handlers.

## Decision

A project declares its templates once, in `templates.yaml`: name, category, language and the body components with their example values, as the WhatsApp template model defines them and with nothing from any provider's API in the file. `socialagent templates sync` (which runs the validator first unless `--skip-validation`) reconciles that file against what the provider has registered: it creates what is missing, skips what exists, re-registers everything only with `--force`, and deletes what the file no longer lists only with `--delete-removed`. The orchestration lives in `src/socialagent/templates/sync.py` and talks to the `MessageProvider` interface alone, through `list_registered_templates`, `register_template` and `delete_template`; the provider class owns every API call, so a second provider implements three methods and changes nothing in `sync.py` or the handlers. `socialagent templates validate` checks the file locally against the rejection causes Meta publishes, and `templates sync` runs the same checks first and refuses to send a template with a blocking finding.

## Alternatives considered

- **Register templates by hand in the provider console.** Not reproducible across environments, not reviewable, and the handler that depends on a template has nothing in the repository to point at. Rejected.
- **Provider API calls in the project's handlers.** Ties the project to one provider and puts the same reconciliation logic in every project. Rejected; the provider abstraction already covers messaging, so covering template sync in the same class is the smaller change.
- **Diff and update registered templates by body.** The provider's listing exposes name, status, language and category but no body text, so a body-level diff is impossible from the listing, and re-registering an unchanged template re-triggers Meta review. Rejected in favour of skip-by-default and an explicit `--force`.
- **Let Meta be the only validator.** A rejection arrives after the deploy has run and reads as a generic parameter error. Rejected; the local checks catch the structural causes before anything is sent, and the density rule stays a warning because Meta does not publish its threshold.

## Consequences

- A project's templates are reviewed like code and synced like code; the CLI reads the phone id, Region and name prefix from the project's `socialagent.<env>.yaml`.
- Every registered name carries the project's prefix (`<environment>_<project>_` unless `whatsapp.template_prefix` says otherwise), applied by the provider on every send and at sync, so two stacks on one WhatsApp Business Account do not collide, and `--delete-removed` refuses to run without a prefix because it would otherwise delete other projects' templates. The notification handler applies the prefix when sending, so a handler must not add it twice.
- A rejection Meta still returns makes `templates sync` exit non-zero, so a failed registration fails the deploy instead of passing silently.
- Keep UTILITY templates transactional. Meta reclassifies a body with emoji or branding as MARKETING, which changes its price and delivery rules; the validator warns.
- The template registry (`src/socialagent/templates/registry.py`) renders a body from the same file so an out-of-band send can write the rendered text to memory (see [ADR-0006](0006-memory-ownership.md)).

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Templates are body-only today; when header, footer and button components arrive, does the YAML stay provider-agnostic or does it need a provider-specific block?

## Enforced by

- `src/socialagent/providers/__init__.py`: `MessageProvider` declares `list_registered_templates`, `register_template` and `delete_template` as abstract methods, so a provider cannot be instantiated without them.
- `src/socialagent/templates/sync.py::sync_templates`: the orchestrator, written against `MessageProvider` only.
- `src/socialagent/templates/validate.py::validate_template` and `::validate_file`: the local checks; `src/socialagent/cli/templates.py` wires them into both `templates validate` and the `before_send` hook of `templates sync`.
- `tests/test_sync.py::test_creates_new_templates`, `::test_skips_already_registered_by_default`, `::test_force_reregisters_existing`, `::test_no_delete_by_default`, `::test_deletes_removed_when_flag_set` and `::test_prefix_applied`: the reconciliation rules, run against a mock provider so they hold for any implementation.
- `tests/test_eum_social.py::test_register_template_calls_create` and `::test_register_template_applies_prefix`: the one shipped provider's side of the contract.
- `tests/test_templates_validate.py::test_variable_at_either_end_is_an_error`, `::test_low_static_text_per_variable_is_only_a_warning`, `::test_emoji_in_utility_is_a_warning_but_punctuation_is_not` and `::test_shipped_blueprint_templates_pass`: the checks, and that every blueprint's `templates.yaml` passes them.
- `tests/test_cli_templates.py::test_sync_refuses_a_template_meta_would_reject`, `::test_sync_exits_non_zero_when_a_template_fails` and `::test_sync_reads_phone_region_and_derived_prefix_from_yaml`: the CLI's guard, exit code and inputs.
