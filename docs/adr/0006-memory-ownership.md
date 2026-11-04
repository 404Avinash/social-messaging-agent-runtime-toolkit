---
id: ADR-0006
title: "The toolkit writes out-of-band messages to memory; the agent writes its own conversation"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [memory, data]
---

# ADR-0006: The toolkit writes out-of-band messages to memory; the agent writes its own conversation

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

AgentCore Memory is the one store the toolkit requires: it holds each user's conversation, and it is the shared bus between the agent and the toolkit's Lambdas. Two parties can write to it. The agent, through its framework's session manager, records the user's message, its own reply and (framework permitting) its tool calls. The toolkit's Lambdas send messages the agent never sees in its loop: a template fired from an external event, a scheduled reminder, a status text from a handler. If nobody wrote those, the agent's next turn would not know the user had been told anything. If both parties wrote the in-loop turns, every message would appear twice.

The toolkit is also framework-agnostic. Strands writes tool exchanges one way, LangGraph another, a custom agent however its author decides. A toolkit that wrote the in-loop conversation would have to agree with every framework's notion of a turn, or impose its own.

## Decision

The agent owns its conversation: user turns, its replies and its tool exchanges are written by the agent's session manager, which `create_session_manager(payload)` configures from the ids the webhook passes (`memory_id`, the stack's Memory resource, and `session_id` and `actor_id`, derived from the phone number so both parties always agree). The toolkit writes to memory only what originates outside the agent loop, always as the assistant role: `EventContext.send_text`, `send_template` and `send_message` in event and scheduler handlers always write after sending, and the notification Lambda writes when a direct `notify` call asks for it with `log_to_memory=True`. The webhook, which handles the in-loop path, holds no memory writer at all and never writes.

## Alternatives considered

- **The toolkit writes everything, including the in-loop turns.** Double-writes with every framework that has its own session manager, and a framework-specific definition of a turn inside the Lambda Control Layer. Rejected.
- **The agent writes everything, including out-of-band sends.** Out-of-band sends happen in a Lambda while no agent turn is running; the agent would have to be invoked just to record them, and a template sent outside the 24-hour window often has no live session to invoke into. Rejected.
- **A memory write on the in-band path, from the queued message's `log_to_memory` flag.** An assistant event written mid-turn is the corruption [ADR-0002](0002-in-band-sends-through-notify-context.md) exists to prevent, and after the turn the agent's own reply is what the user saw. Rejected; the webhook drain ignores the flag.

## Consequences

- `log_to_memory` and `memory_text` do nothing on a queued in-band message. The agent's reply text, which its session manager records, is what persists. A tool result should say what was sent so the reply can mention it.
- On restore, `packages/socialagent-runtime/_history.py` strips reasoning, image, document and tool blocks and keeps an assistant turn made only of delivered tool calls as the text the user saw (the SDK's own `filter_restored_tool_context` stays off). What the agent reads back is the conversation as both sides saw it, not the tool traffic.
- `immediate=True` cannot be combined with `log_to_memory=True`; the call raises. A send that must be remembered and must reach another phone belongs in an event or scheduler handler, where `EventContext` always writes.
- Frameworks other than Strands record no tool exchange by themselves; a project using `create_memory_client` logs tool calls itself. `docs/agent-integration.md` shows the pattern.
- `ctx.write_to_memory` takes no role: it is assistant-only by design, because the toolkit never speaks for the user.
- The writer keeps its event timestamps strictly increasing per session within one container, because the wire format carries whole seconds and two out-of-band writes in the same second would otherwise restore in service-determined order.
- In dev mode every write is a no-op against AWS and lands in the REPL's local transcript instead, so `socialagent dev` runs without a memory resource.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- In-band queued messages are delivered but never written to memory (`log_to_memory` is ignored on that path). Is that a gap to close or the correct boundary?

## Enforced by

- `packages/socialagent-runtime/_history.py::strip_replay_unsafe`: the restore filter described above, applied by both the AgentCore and the local file session managers.
- `src/socialagent/memory/writer.py::SocialAgentMemoryWriter.log_assistant_message`: the only method, assistant role only; the class docstring states the ownership rule.
- `src/socialagent/handlers/common.py::send_and_log` and `::log_delivered`: the shared send-then-write step delivers first and never lets a failed write undo a delivered message; `log_delivered` is that guarded write on its own, for paths that send through another call. `src/socialagent/handlers/event.py` uses them from `EventContext.send_text`, `send_message` and `send_template`, and `write_to_memory` writes without sending; `src/socialagent/handlers/notification.py` calls it only when `log_to_memory` is set.
- `src/socialagent/handlers/webhook.py::make_webhook_handler`: takes a provider and no memory writer; the module never imports one.
- `packages/socialagent-runtime/__init__.py::create_session_manager`: configures the agent's own session manager from the payload's ids.
- `tests/test_event.py::test_send_text_sends_and_logs`, `::test_send_message_with_memory_text`, `::test_write_to_memory_assistant` and `::test_write_to_memory_user_role_skipped`: handlers always write, as the assistant.
- `tests/test_notification.py::test_send_and_log_text` and `::test_send_and_log_swallows_memory_failure_after_delivery`: the out-of-band write, and that a failed write never undoes a delivered message.
- `tests/test_notify_immediate.py::test_send_text_immediate_with_log_to_memory_raises`: no mid-turn write from the agent side.
- `tests/test_memory_writer.py::test_consecutive_writes_get_distinct_wire_timestamps` and `::test_dev_mode_never_calls_create_event`: ordering, and the dev-mode no-op.
