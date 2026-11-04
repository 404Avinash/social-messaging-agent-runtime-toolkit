# Notifications

How to send outbound WhatsApp messages from agents and handlers, and when to write to AgentCore Memory.

---

## Agent Runtime

The agent runs inside AgentCore Runtime. Tools execute during an active agent turn, which means an LLM `toolUse`/`toolResult` exchange is in flight in AgentCore Memory.

### The `outbound_context` requirement

Every `notify.*` call from a tool **must** run inside `socialagent_notify_context()`. Calling `notify` directly from a tool corrupts the AgentCore Memory session by injecting an ASSISTANT event between `toolUse` and `toolResult`.

```python
# agent.py — required pattern
with socialagent_notify_context() as outbound:
    result = agent(payload["prompt"], ...)
return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

Inside `socialagent_notify_context()`, `notify.send_text`, `send_message` and `send_template` calls are **queued** and returned in `outbound_messages`. After the turn completes, the webhook sends each queued message through the provider directly, then the agent's text reply. The notification Lambda is not involved in that drain. Mid-turn, only `immediate=True` sends, `send_typing` and `upload_media` reach it, because they bypass the queue; otherwise it serves the out-of-band path below.

Whichever path a message takes, its recipient is the phone handed to the provider (for a queued send, the conversation's sender), never one inside the message: the provider drops `to`, `type`, `messaging_product` and `recipient_type` from a message payload, with a warning, before it builds the WhatsApp request.

### Memory writes from tools

**Same-phone sends** (tool replies to the current user): `log_to_memory` has no effect, because the webhook drain never reads it. The framework's session manager records the tool exchange, but a `toolResult` stays in the agent's context for the current turn only: on restore, `socialagent_runtime._history` strips past `toolUse`/`toolResult` blocks and keeps a tool-only assistant turn as the text the user saw (the SDK's own `filter_restored_tool_context` stays off, because it drops the message it empties). That applies in a deployed stack; under `socialagent dev` it returns a local `FileSessionManager`, which keeps complete tool exchanges so a local run can recall one that a deployed agent would not. Both managers discard restored `reasoningContent` blocks because those blocks are bound to the model context that produced them and are unsafe to replay after the system prompt or model changes. Make the result descriptive so the agent's reply can say what was sent; that reply text is what persists across turns:

```python
# ❌ Agent has no idea what was sent
return {"sent": True}

# ✅ Agent knows exactly what was delivered
return {"sent": True, "message_type": "buttons", "options": ["Tuesday 10am", "Wednesday 2pm"]}
```

**Cross-phone sends from a tool need `immediate=True`.** A queued send is always delivered to the conversation's sender — the webhook drain ignores any `phone` you pass, so a prompt-injected agent cannot redirect replies. `immediate=True` bypasses the queue and sends to the phone you name, but it cannot write to memory (`log_to_memory=True` raises, because a mid-turn memory write corrupts the session). Nothing redirects an `immediate=True` send to the sender, so never take its recipient from model input: use the current user's phone from `tool_context.invocation_state["phone"]`, or a number your own code looks up, never a `phone` tool argument the LLM fills in. If the recipient's agent needs context on its next turn, notify from an event or scheduler handler instead: those run out-of-band through `EventContext`, which always writes memory (see [Memory writes from handlers](#memory-writes-from-handlers)).

```python
# Same-phone — queued, no flag needed
await notify.send_message(phone, result["message"])

# Cross-phone from a tool — immediate, and no memory write is possible.
# other_phone comes from your own data (e.g. the listing's seller), not the model.
await notify.send_template(
    other_phone,
    "seller_new_inquiry",
    params=[property_name, slots_summary],
    immediate=True,
)
```

> **Non-Strands frameworks**: only Strands records tool exchanges for you (`AgentCoreMemorySessionManager`). For LangGraph, CrewAI, or custom agents using `create_memory_client`, call `mem.log_tool_call(...)` after every tool-initiated send. `log_to_memory=True` is no substitute: the send is queued, and the webhook drain never reads the flag. See [Agent Integration](agent-integration.md#custom-agents) for the full pattern.

### Cross-phone constraints

WhatsApp only allows initiating a conversation with a **template**. Text and rich messages require an active 24-hour window.

| Method | Cross-phone safe? |
|--------|------------------|
| `send_template` | ✅ Always |
| `send_text` | ⚠️ Only within active window |
| `send_message` | ⚠️ Only within active window |

**Best practice:** always use `send_template` for cross-phone notifications.

### The four methods

```python
import socialagent_runtime.notify as notify

await notify.send_text(phone, text, *, log_to_memory=False, immediate=False)
await notify.send_message(phone, message, *, log_to_memory=False, memory_text="", immediate=False)
await notify.send_template(phone, template_name, params, *, language="es_MX", log_to_memory=False, immediate=False)
await notify.send_typing(phone, message_id)
```

`send_message` with `log_to_memory=True` requires `memory_text` — rich payloads have no auto-renderable representation:

```python
# Raises ValueError
await notify.send_message(phone, msg, log_to_memory=True)

# Correct
await notify.send_message(phone, msg, log_to_memory=True, memory_text="[Sent slot options]")
```

`send_template` with `log_to_memory=True` auto-renders the body from the template registry — no `memory_text` needed.

### Sending mid-turn with `immediate=True`

By default, a `notify.send_text`/`send_message`/`send_template` call made
inside `outbound_context()` is **queued** and only delivered by the webhook
once the whole turn finishes (see [The `outbound_context` requirement](#the-outbound_context-requirement)
above). `immediate=True` opts a single call out of that queue: it dispatches
to the notification Lambda the instant you call it, even from the middle of
a tool, so the user sees it *before* a slow operation returns rather than
bundled with the tool's final result at the end of the turn.

```python
@tool(context=True)
async def generate_report(tool_context: ToolContext) -> str:
    phone = tool_context.invocation_state["phone"]
    await notify.send_text(phone, "Generating your report, this may take a minute…", immediate=True)
    report = await slow_report_generation()
    return report
```

Delivery is fire-and-forget (`InvocationType="Event"`) and non-fatal — a
failed immediate send is logged and swallowed, never raised, so a cosmetic
progress message can never break the tool that sent it. A non-immediate
send outside `outbound_context()` still raises on
failure as normal; only the `immediate=True` path is non-fatal.

**`immediate=True` and `log_to_memory=True` cannot be combined — `ValueError` is raised if you try.** An immediate mid-turn send that also wrote to memory would recreate the exact corruption problem `outbound_context()` exists to prevent (an assistant event landing between `toolUse` and `toolResult`). Immediate sends are transient/UX-only by design. If what you send must be in memory, don't use `immediate=True`: for the current user, let the call queue and have the agent mention it in its reply, which is what persists (see [Memory writes from tools](#memory-writes-from-tools)); for any other phone, send it from an event or scheduler handler instead, where `EventContext` always writes memory.

### Refreshing the typing indicator with `send_typing`

`notify.send_typing(phone, message_id)` re-emits WhatsApp's "typing…"
indicator, which expires after roughly 25 seconds. The webhook triggers it
once automatically when a message arrives, but a tool slower than that
(image generation, a large upload, a slow external API) lets it lapse
mid-turn, making the bot look unresponsive. Call it again — using the
`message_id` from `payload["socialagent"]["message_id"]` — to refresh it during a
long-running tool:

```python
await notify.send_typing(phone, payload["socialagent"]["message_id"])
```

Unlike the four methods above, `send_typing` takes no `log_to_memory` or
`immediate` flag: it **always** fires immediately, even inside
`outbound_context()`, and **never** writes to memory, so it can never cause
the `toolUse`/`toolResult` corruption in the first place. Non-fatal, no-op
in dev mode.

---

## Handlers (Scheduler / Event)

Handlers run outside the agent turn — no active AgentCore Memory session, no `toolUse`/`toolResult` in flight. Use `EventContext` methods, not `notify.*` directly.

```python
@app.on_schedule("appointment_reminder")
async def handle_reminder(data: dict, ctx: EventContext) -> None:
    await ctx.send_template(
        data["phone"],
        "appointment_reminder",
        params=[data["property_name"], data["time"], data["location"]],
        language="en",
    )
```

### Memory writes from handlers

`EventContext` **always writes to memory** after sending — no flag needed. This is safe because there is no active session to corrupt.

| Method | Sends message | Writes memory |
|--------|--------------|---------------|
| `ctx.send_text(phone, text)` | ✅ | ✅ Always |
| `ctx.send_template(phone, name, params, lang)` | ✅ | ✅ Always (rendered body) |
| `ctx.send_message(phone, message, memory_text="")` | ✅ | ✅ Always (`memory_text`, or `[<type> message]` when empty) |
| `ctx.write_to_memory(phone, text)` | ❌ | ✅ Always (records something without sending it) |

### Cross-phone constraints

Same rules apply — use `send_template` for any phone that may not have an active window. Scheduler reminders and event notifications are almost always cross-phone and out-of-window, so templates are the correct choice.

---

## Summary table

| # | Sender | Recipient | Method | Memory written? |
|---|--------|-----------|--------|-----------------|
| 1 | Tool | Same phone | any | Recorded, but stripped on restore; only the agent's reply text persists |
| 2 | Tool | Different phone | any, with `immediate=True` | No — `immediate` forbids `log_to_memory` |
| 3 | Handler | Any phone | `ctx.send_text` | Always |
| 4 | Handler | Any phone | `ctx.send_template` | Always |

A queued tool send (the default inside `socialagent_notify_context()`) always goes to the conversation's sender, so row 2 needs `immediate=True` — see [Memory writes from tools](#memory-writes-from-tools).
