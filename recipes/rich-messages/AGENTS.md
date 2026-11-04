# AGENTS.md — rich-messages recipe

AI-optimized wiring guide. Goal: attach WhatsApp interactive elements (buttons, lists, media, location) to agent replies.

## Files copied by `socialagent recipe add rich-messages`

| Source | Destination | Purpose |
|--------|-------------|---------|
| `tool.py` | `src/tools/rich_messages.py` | Pure-Python `build_rich_message(message_type, ...)` payload builder |
| `skill.md` | `src/skills/rich_messages.md` | Agent guidance; references a tool named `attach_interactive` |

## Wiring steps

1. Import `build_rich_message` from the copied tool module into `src/tools/__init__.py`.
2. Define a Strands tool named exactly `attach_interactive` (the skill references this name) decorated with `@tool(context=True)`.
3. In the tool: call `build_rich_message(message_type, ...)`. If the result has an `"error"` key, `raise ValueError(result["error"])` so the agent sees the failure and can retry. Otherwise send the already-wrapped payload via `await notify.send_message(tool_context.invocation_state.get("phone", ""), result["message"])`.
4. Ensure the agent entrypoint runs inside `socialagent_notify_context()` so the payload is queued and drained by the webhook after the turn (do not send mid-turn).
5. If you rename the tool, update `skill.md` so the name matches.

## Supported message types

`buttons`, `list_menu`, `link_button`, `image`, `video`, `location`, `location_request`. The builder returns the correctly-wrapped `{"type", "payload"}` shape that `notify.send_message` expects — pass `result["message"]` through unchanged.

> `call_button` and `address_request` are not supported — WhatsApp silently drops both on standard WABA tiers.

## Required env vars

None. The builder reads no environment variables.

## Required IAM actions

None. The builder makes no AWS calls. Delivery uses the existing SMART (Social Messaging Agent Runtime Toolkit) notify path, which already has the necessary `socialmessaging` permissions — no new IAM statements required.
