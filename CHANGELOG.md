# Changelog

All notable changes to SMART, the Social Messaging Agent Runtime Toolkit, are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are calendar-based, `YYYY.MM.patch`, so `2026.10` is the release line of October 2026, `2026.10.0` is its first release, and `2026.10.1` would be a fix to it; the version lives in the `VERSION` file at the repository root and nowhere else. The admin console has its own heading under each release because a console-only release is a real case.

## [Unreleased]

### Fixed
- `personal-assistant` now passes `transcribe_languages` to the transcription call in its `on_media` hook, so the setting applies to its voice notes as it does to the default path. `handlers/` is project code, so an existing project adds `language_options=config.transcribe_languages,` to the `transcribe_audio` call in `handlers/webhook.py` by hand. The blueprint's YAML sets `transcribe_languages: [es-MX, en-US]`, which had no effect on its voice notes before this fix, so check that the list names your users' languages before you deploy the change.

### Admin console
- The State tab and the global State page no longer list or count the scheduler's `_schedule#` firing records as user state; Nuclear reset leaves them in place, as they expire on their own.
- The conversation viewer's "Delete conversation" asks for confirmation inside the viewer, naming the conversation and warning that the deletion cannot be undone, before it deletes the conversation's AgentCore Memory events; one click no longer deletes.
- The Stack Detail Deploys tab reports a failed CodeBuild read instead of showing an empty history, reads each stack's builds in the stack's own region, and reads a zip stack's builds from `<prefix>-agent-zip-build`.

## [2026.10.0] - 2026-11-05

The first public release. Everything below was in the toolkit when it was published.

### WhatsApp channel
- Outbound message types: text, buttons, list menus, image, document, audio, video, sticker, location, location request, CTA URL, address request, call button.
- Inbound text, media, interactive replies (button, list row, form) and the typing indicator.
- Audio transcription on inbound voice notes.
- Templates: body-only templates in the UTILITY, MARKETING and AUTHENTICATION categories, synced on deploy, with local validation of the rejection causes Meta publishes; a rejection fails the deploy.
- Message buffering: rapid inbound messages are debounced into one agent turn.
- A per-user rate limit, when the state table is enabled.

### Agent runtime
- Recurring and one-shot schedules owned by phone, with ownership read from one field of the schedule name, restricted operation names, a cron floor and a per-user cap.
- External events through API Gateway with an API key and a secret, with credentials sent only to this account's API bound to the key.

### Developer experience
- The `socialagent` CLI: `init`, `dev`, `recipe`, `templates`, `config`, `bootstrap`, `deploy`, `destroy`, `status`, `secrets`.
- Three blueprints (`minimal`, `personal-assistant`, `appointment-scheduler`) and three recipes (`rich-messages`, `voice-reply`, `generate-image`), copied into a project as files.

### Operations
- Security review follow-ups: the SNS certificate URL is parsed, not matched; the WhatsApp envelope is pinned; media roots are allowlisted; invocation logs carry metadata only; roles are scoped to the stack's media keys; console authorization is single-layered.
- Dependabot with cooldowns on every ecosystem: pip, npm, GitHub Actions, Terraform.
- An anonymous deployment counter: one CloudFormation marker stack per deployment, stated in the README and printed by `socialagent deploy`, and turned off with `terraform.enable_deployment_metrics: false`.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `NOTICE`, and Apache-2.0 license headers on every file.

### Admin console (beta)
- Fleet view, conversations, per-user state, schedules, templates with preview, logs, errors, restart, phone binding; Cognito sign-in with MFA; deny-by-default authorization on every route. The console has had less production time than the SDK and CLI and ships as beta; it is a separate Terraform root an adopter may skip.

[Unreleased]: https://github.com/awslabs/social-messaging-agent-runtime-toolkit/compare/v2026.10.0...HEAD
[2026.10.0]: https://github.com/awslabs/social-messaging-agent-runtime-toolkit/releases/tag/v2026.10.0
