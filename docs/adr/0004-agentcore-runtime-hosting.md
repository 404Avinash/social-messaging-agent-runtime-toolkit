---
id: ADR-0004
title: "Managed hosting is AgentCore Runtime; an external-runtime mode is planned, not shipped"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [hosting, runtime]
---

# ADR-0004: Managed hosting is AgentCore Runtime; an external-runtime mode is planned, not shipped

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

The toolkit is agnostic on agent framework, model and business logic, and opinionated on the channel. Somewhere that opinion has to include where the agent runs: the webhook Lambda needs a stable way to invoke it with the conversation's payload, keep concurrent messages from one user in order, and get back the text reply plus the messages the agent queued during its turn (see [ADR-0002](0002-in-band-sends-through-notify-context.md)). Sessions also need to outlive one message so a restored history is the same whichever Lambda instance handled the previous turn.

Amazon Bedrock AgentCore Runtime gives all of that as a service: a container or zip the toolkit builds and deploys, per-session microVMs keyed by `runtimeSessionId` so one user's messages are serialized, and the sibling AgentCore Memory service the toolkit already requires for conversation persistence. Supporting a second managed host (a Lambda, an ECS service, a container the adopter runs) would mean a second build pipeline, a second IAM model, a second way to serialize sessions and a second set of Terraform to keep in step.

## Decision

The toolkit builds, deploys and invokes exactly one managed host: AgentCore Runtime. `socialagent deploy` provisions it from the project's Dockerfile (`deployment_type = "docker"`) or as a Python zip (`"zip"`), the webhook invokes it through the `bedrock-agentcore` API with the session id as `runtimeSessionId`, and `AGENT_RUNTIME_ARN` is a required setting of the Lambda Control Layer.

An agent that must run somewhere else is not a second managed host. It will be reached through an **external-runtime mode**: the webhook calls an HTTPS endpoint the adopter runs, with the same payload and the same `SocialAgentResponse` contract, and the adopter owns the hosting, scaling and session serialization behind it. That mode is planned and not shipped. Today there is no configuration option, no code path and no test for it, and `ROADMAP.md` does not yet carry a row for it; this record is the only place it is written down. Nothing in the toolkit should be read as supporting an external agent until it does.

## Alternatives considered

- **Host-agnostic adapters: Lambda, ECS, a plain container, with AgentCore as one of several.** Each adapter is a build pipeline, an IAM role set, a Terraform module and a session-serialization story of its own, and the webhook would carry a dispatch over them. Rejected: the toolkit's value is a channel that works end to end, not a menu of hosts.
- **The agent runs inside the webhook Lambda.** No separate runtime to deploy, but a Lambda's duration limit, cold start and lack of per-user session affinity make it a poor fit for an agent turn, and it would pull the agent framework into the Lambda layer, against [ADR-0001](0001-two-runtime-layers.md). Rejected.
- **Ship the external-runtime mode now, alongside AgentCore.** The contract it needs (payload shape, response shape, how the adopter proves the endpoint is theirs, how sessions are kept in order without `runtimeSessionId`) is not settled, and shipping half of it would commit the toolkit to a surface it would have to keep. Deferred; recorded here so the hosting decision is not read as a ban.

## Consequences

- An adopter needs AgentCore Runtime and AgentCore Memory available in their Region. The toolkit documents nothing for other hosts.
- Runtime sessions keep the version they started with until they end, after 15 idle minutes or at most 8 hours after they started (AgentCore's defaults, which the stack does not override). Testing a deploy means stopping the session first; `AGENTS.md` and `docs/architecture.md` spell out the command.
- The Terraform never force-replaces the runtime: deletion is slow and the recreate collides on the name. In-place updates only.
- When the external-runtime mode ships, this record stays in force for managed hosting, and a new record describes the mode's contract and supersedes the "planned, not shipped" paragraph above.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Is AgentCore exclusivity the right default for the managed path, or should a second managed host (ECS behind an ALB is the one asked for most) be a build target rather than an external endpoint? This record says no; it is the decision most worth challenging in review.
- For the external-runtime mode: what is the minimum contract (payload, response, auth, session ordering) that keeps the webhook unchanged?

## Enforced by

- `src/socialagent/config.py`: `SocialAgentConfig.from_env()` raises when `AGENT_RUNTIME_ARN` is unset.
- `src/socialagent/handlers/webhook.py::_invoke_runtime`: the webhook's invocation path, a `bedrock-agentcore` client call with `runtimeSessionId`. The `personal-assistant` blueprint's `handlers/scheduler.py` calls `invoke_agent_runtime` directly for scheduled agent tasks, in sessions of their own.
- `infra-terraform/modules/runtime/codebuild.tf`: the `aws_bedrockagentcore_agent_runtime` resource and the CodeBuild project that builds the image or zip for it.
- `tests/test_config.py::test_from_env_missing_both` and `::test_from_env_all_required`: the required setting and its value.
- `tests/test_webhook.py::test_handler_sends_outbound_messages_before_text` and the rest of that file patch `_invoke_runtime`, which is the seam an external-runtime mode would have to fit behind.
