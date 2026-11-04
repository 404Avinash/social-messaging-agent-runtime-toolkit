---
id: ADR-0001
title: "Two runtime layers that never import each other"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [architecture, layering]
---

# ADR-0001: Two runtime layers that never import each other

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

The toolkit's code runs in two places that share nothing at runtime. The Lambda Control Layer (`src/socialagent/`) runs in the webhook, notification, event and scheduler Lambda functions; it is built into a shared Lambda layer and owns webhook parsing, the WhatsApp provider, memory writes, scheduling and template sync. The Agent Runtime Package (`packages/socialagent-runtime/`) runs inside the AgentCore Runtime container next to the developer's agent, where it is copied in as `socialagent_runtime/` (see [ADR-0003](0003-agent-runtime-is-vendored.md)), and gives the agent `notify`, `state`, `scheduler`, `messages` and the session helpers.

An import from one layer into the other would pull the whole dependency tree of one container into the other: the agent image would carry the provider, boto3 clients for services the agent never calls, and the Lambda handler code; the Lambda layer would carry the agent framework helpers. Worse, it would blur who may call the WhatsApp API. The provider is the only place that builds a `socialmessaging` client, and the only way agent-side code reaches it is a network call to the notification Lambda or a message queued for the webhook to send.

## Decision

`src/socialagent/` and `packages/socialagent-runtime/` never import each other at module level, in either direction; the only crossings are guarded function-level imports that run under `socialagent dev` and are a no-op when the other package is absent. Recipe `tool.py` files and blueprint agent code run agent-side and import only `socialagent_runtime` and the standard library. The one crossing between the layers is at runtime, not at import time: `socialagent_runtime.notify` invokes the notification Lambda, and queued messages travel back to the webhook in the runtime's response payload.

## Alternatives considered

- **One package installed in both containers.** Simplest to publish, but the agent image would carry every Lambda dependency and the Lambda layer would carry agent-side ones, and nothing would stop a tool from building a WhatsApp client directly. Rejected.
- **A shared `common` package both layers depend on.** Keeps the layers apart on paper, but every shared module becomes a place where a provider call can be hidden, and the payload contract between the two containers would be an implicit import rather than an explicit JSON shape. Rejected; the contract is the payload, versioned by `socialagent.version`.
- **Trust review to keep the boundary.** External contributors have not read `AGENTS.md`, and the import that crosses the line is the one that looks most convenient at the time. Rejected in favour of tests whose failure message names the rule and the fix.

## Consequences

- A helper the agent needs and the Lambda also needs is written twice, or lives in whichever layer owns the data it touches. `normalize_phone` exists in both `src/socialagent/utils/phone.py` and `packages/socialagent-runtime/phone.py` for this reason.
- The payload that crosses the boundary is a documented contract: the webhook builds it, the runtime package reads it, and `socialagent.version` lets the runtime refuse a shape it does not understand.
- Porting the agent side to another framework touches only `agent.py` and a project's `src/tools/`; the recipe `tool.py` logic and `socialagent_runtime` stay the same, because neither knows about the Lambda side.
- A change that needs to cross the boundary is a signal the design is wrong, not that the test needs an exemption. `make test-arch` runs the rules on their own.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Should the recipe `tool.py` files be allowed a third, explicitly listed import (for example a shared validation helper) without opening the whole `src/` tree? Today the answer is no, by test.

## Enforced by

- `tests/test_architecture.py::test_agent_package_does_not_import_control_layer`: no module under `packages/socialagent-runtime/` imports `socialagent.*`.
- `tests/test_architecture.py::test_control_layer_does_not_import_agent_package`: no module under `src/socialagent/` imports `socialagent_runtime`.
- `tests/test_architecture.py::test_recipes_do_not_import_control_layer`: every `recipes/*/tool.py` imports only `socialagent_runtime` and the standard library.
- `tests/test_architecture.py::test_socialmessaging_client_only_in_provider`: the `socialmessaging` client is built only in `src/socialagent/providers/eum_social.py`, so the WhatsApp API has one caller.
- `tests/test_architecture.py::test_layer_rules_cover_real_files`: the rules above scan real files, so an empty match cannot pass them vacuously.
- `tests/test_provider_isolation.py::test_socialagent_runtime_no_forbidden_imports` and `::test_no_direct_socialmessaging_calls`: older, substring-based checks kept as a second net; the first forbids only the retired package namespaces, the second the direct client construction.

The checks are AST-based, so quote style, whitespace, `session.client(...)` and a `service_name=` keyword do not matter. Imports inside a function are exempt on purpose (the guarded dev-mode bridges), and a service name held in a variable is not detected.
