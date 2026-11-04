---
id: ADR-0002
title: "In-band sends are queued through `socialagent_notify_context()` and delivered to the conversation's sender"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [messaging, security]
---

# ADR-0002: In-band sends are queued through `socialagent_notify_context()` and delivered to the conversation's sender

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

A tool that runs during an agent turn runs while an LLM `toolUse`/`toolResult` exchange is in flight in AgentCore Memory. If that tool sent a WhatsApp message by invoking the notification Lambda directly and the Lambda wrote the message to memory, an extra assistant event would land between `toolUse` and `toolResult`. The session is then malformed, and the next invocation fails with a validation error from the model API. The toolkit ships no repair logic for such a session: once the events are written in the wrong order there is no reliable way to know what the history should have been.

The same path is also where a prompt injection would try to redirect a reply. `notify` takes a `phone` argument that a tool passes and the model may have filled in; if the delivery path honoured it, a message crafted by one user could make the agent send to any number.

## Decision

Every message a tool sends during an agent turn goes through `socialagent_notify_context()`. Inside the context, `notify.send_text`, `send_message` and `send_template` do not call the Lambda: they append to a per-invocation queue, which the entrypoint returns as `SocialAgentResponse.outbound_messages`. The webhook Lambda sends the queued messages after the turn completes and before the agent's text reply, and every queued message is delivered to the conversation's sender. A `phone` on a queued message that differs from the sender is logged and ignored, and a `to` inside a message payload is dropped with a warning. `immediate=True` bypasses the queue for the cases that need it (typing indicators, a send that must survive a failed turn, a send to another number), and refuses `log_to_memory=True`, so no mid-turn path writes memory.

## Alternatives considered

- **Direct `notify` calls from tools, with a memory repair step.** Detecting and repairing a mismatched `toolUse`/`toolResult` pair after the fact is guesswork about what the session should have been. Rejected; the queue removes the cause instead of treating the symptom.
- **Honour the `phone` the tool passes, and trust the system prompt to keep the model honest.** A system prompt is advice to the model, not a control; a user's message is untrusted input. Rejected. The recipient is pinned Lambda-side where model output cannot reach it.
- **A `msg["phone"]` fallback in the drain for the legitimate cross-phone case.** Any fallback the model can trigger is the redirect it was meant to prevent. Rejected; cross-phone sends use `immediate=True` with a recipient taken from `tool_context.invocation_state["phone"]` or project code, never from a tool argument.

## Consequences

- Every `agent.py` whose tools send messages must wrap the agent call in `with socialagent_notify_context() as outbound:` and return `SocialAgentResponse(result, outbound_messages=outbound)`. Without the context, a `notify` call from a tool goes out-of-band through the Lambda, which is the corruption path.
- `log_to_memory` and `memory_text` have no effect on a queued message: the webhook drain never reads them. The text the agent says in its reply is what persists across turns (see [ADR-0006](0006-memory-ownership.md)).
- If the agent turn raises, the queue is dropped with it and the user gets the generic error text. A send that must survive that uses `immediate=True`, whose failures are logged and swallowed.
- The webhook sends consecutive messages `outbound_gap_seconds` apart, because WhatsApp can deliver back-to-back messages out of order.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- `immediate=True` bypasses the queue and pins no recipient; is a project-side allow-list of recipients worth the complexity, or is "take the phone from `invocation_state`, never from a tool argument" enough?

## Enforced by

- `packages/socialagent-runtime/notify.py`: `outbound_context()` sets a `ContextVar` queue; `send_text`, `send_message` and `send_template` append to it when set and `immediate` is false, and raise when `immediate=True` is combined with `log_to_memory=True`.
- `src/socialagent/handlers/webhook.py::_invoke_and_respond`: drains `outbound_messages` to the sender, logging and ignoring a differing `phone`.
- `tests/test_agent_messages.py::test_outbound_context_queues_instead_of_sending` and `::test_outbound_context_does_not_affect_outer_scope`: the queue captures sends inside the context and nothing outside it.
- `tests/test_notify_immediate.py::test_send_text_non_immediate_still_queues_inside_context`, `::test_send_template_non_immediate_still_queues_inside_context`, `::test_send_text_immediate_bypasses_queue_inside_context` and `::test_send_text_immediate_with_log_to_memory_raises`: the queue, the bypass, and the refusal to write memory mid-turn.
- `tests/test_webhook.py::test_handler_sends_outbound_messages_before_text` and `::test_handler_pins_queued_recipient_to_sender`: order of delivery, and that a differing `phone` is pinned to the sender and logged.
- `tests/test_outbound_envelope.py::test_webhook_drain_payload_cannot_redirect_a_queued_message`, `::test_notification_payload_cannot_redirect_from_data_phone` and `::test_scheduled_message_payload_cannot_redirect_from_event_phone`: a `to` inside a payload cannot override the recipient on any delivery path.
