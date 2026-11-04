# rich-messages

Attach WhatsApp interactive elements (buttons, list menus, media, locations) to your agent's replies.

## Overview

The `rich-messages` recipe ships a framework-agnostic builder, `build_rich_message(message_type, ...)`, that turns simple arguments into a validated WhatsApp interactive message payload. It supports `buttons`, `list_menu`, `link_button`, `image`, `video`, `location`, and `location_request`.

> `call_button` and `address_request` are deliberately not supported — WhatsApp silently drops both on standard WABA tiers, so a message built from them would never arrive.

The builder returns `{"message": {...}}` on success or `{"error": "..."}` on failure. It does not send anything itself — your project wraps it in a tool that queues the payload through `socialagent_runtime.notify` so the webhook can deliver it after the agent turn completes. The accompanying skill teaches the agent to treat the attachment as its primary reply (named `attach_interactive` in the skill text).

## Files

`socialagent recipe add rich-messages` copies these files into the enclosing project (the nearest directory, from the current one upward, with an `agent.py` or a `socialagent.<env>.yaml`; the current directory if there is none):

| Source | Destination |
|--------|-------------|
| `tool.py` | `src/tools/rich_messages.py` |
| `skill.md` | `src/skills/rich_messages.md` |

`tool.py` exports the pure-Python `build_rich_message(...)` builder (no framework deps). `skill.md` is the agent-facing guidance that describes when and how to use the interactive elements.

## Wiring

The recipe ships a builder, not a tool. Expose it as a Strands tool named `attach_interactive` (the exact name the skill references) in your project's `src/tools/__init__.py`. The builder already returns the correctly-wrapped payload, so pass `result["message"]` straight to `notify.send_message`:

```python
from strands import tool, ToolContext
from src.tools.rich_messages import build_rich_message
import socialagent_runtime.notify as notify

@tool(context=True)
async def attach_interactive(
    message_type: str,
    tool_context: ToolContext,
    body: str = "",
    options: str = "",
    url: str = "",
    caption: str = "",
    # ... remaining build_rich_message params
) -> dict:
    result = build_rich_message(message_type, body=body, options=options, url=url, caption=caption)
    if "error" in result:
        raise ValueError(result["error"])  # surfaces to the agent so it can correct and retry
    # Queued via socialagent_notify_context() so it is sent after the turn completes.
    await notify.send_message(tool_context.invocation_state.get("phone", ""), result["message"])
    return {"sent": True}
```

`build_rich_message` returns `{"error": ...}` on bad input; the wrapper above turns that into a raised `ValueError` so the agent sees the failure and can retry with corrected arguments. The tool must run inside `socialagent_notify_context()` (the standard `agent.py` entrypoint pattern) so the message is queued and drained by the webhook Lambda rather than sent mid-turn. If you rename the tool, update `skill.md` to match.

## Environment Variables

None. The builder is pure Python and reads no environment variables.

## IAM

None. `build_rich_message` performs no AWS calls. Outbound delivery is handled by the existing SMART (Social Messaging Agent Runtime Toolkit) notify path (webhook/notification Lambda), which already holds the required permissions — the recipe adds no new IAM requirements.
