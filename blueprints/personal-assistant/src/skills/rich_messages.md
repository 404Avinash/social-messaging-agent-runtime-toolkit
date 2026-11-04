---
name: rich_messages
description: Use attach_interactive to send WhatsApp interactive elements (buttons, lists, media, location). The attachment IS your reply — your final text output is optional garnish delivered after.
---

# Rich Messages

## Mental model

When you call `attach_interactive`, the **attachment is sent first, then your final text output**. The attachment carries your full reply — greeting, context, and the question that ends in a choice.

```
attach_interactive(body="...", ...)  →  delivered first  (YOUR MESSAGE)
your final text output                →  delivered second (optional follow-up)
```

**The `body` is your actual reply.** Write it exactly as you would write a text message — warm, conversational, ending with the prompt for the options. Up to 1024 characters. This is the unit the user sees first and reads in full.

**The final text output is optional garnish.** Use it for a short follow-up tip, warmth, or next-step hint. It is fine to leave it empty or very brief when the attachment speaks for itself.

## Examples

### ✅ Good — body is the full reply; final text is optional garnish

```
attach_interactive(
  message_type="list_menu",
  body="Here are the available options — tap one to continue 👇",
  options="Options|Option A~First choice;Option B~Second choice;Option C~Third choice"
)

final text: "Let me know if you need more details on any of these."
```

### ❌ Bad — body is a weak label; text re-introduces what the user already saw

```
attach_interactive(body="Pick one:", options="...")
final text: "Here are the options. Let me know which one works for you!"
```
The body is wasted. The final text re-introduces an attachment the user already received.

## When to use attach_interactive

- **2–3 choices, mutually exclusive** → `buttons`
- **4–10 choices** → `list_menu`
- **Clickable URL** → `link_button`
- **Photo/video** → `image` / `video`
- **Map pin** → `location`
- **Ask user's location** → `location_request`

Never list choices as plain text when an interactive element is available.

## attach_interactive parameters

| Type | Required | Notes |
|------|----------|-------|
| `buttons` | body, options | options = comma-separated labels (2–3, ≤20 chars each, mutually exclusive) |
| `list_menu` | body (non-empty), options | options = `Section\|row~desc;row~desc` or `row~desc;row~desc` |
| `link_button` | body, url | button_text optional (default "Open") |
| `image` | url | caption optional — caption is the body for media |
| `video` | url | caption optional |
| `location` | latitude, longitude | name, address optional |
| `location_request` | body | — |

## Hard rules

1. The `body` is your reply. Write it as a complete, conversational message — not a label.
2. Your final text output is optional. When you do write it, keep it short and do NOT re-introduce the attachment the user already saw above.
3. Max 10 list rows total. Row title ≤24 chars, description ≤72 chars.
4. Row title and description MUST be separated by `~`, never `:`. Dates and times contain `:` — using it as a separator will break parsing. ✅ `May 7 10:00 AM~Morning` ❌ `May 7 10:00 AM:Morning`
5. Locations → coordinates, not URLs.
6. Links → `link_button`, not pasted URLs in the body or text.
7. On tool error → the tool call raises; correct the arguments and retry.
8. Button labels must be distinct and non-redundant. ✅ `Yes,No` ❌ `Yes,Confirm,No` (Confirm and Yes mean the same thing). Each button should represent a meaningfully different action.
9. Every list row must read differently from the others. A tap returns only the row's title and description, never its section, so when the same time appears under two days, put the day in the row itself. ✅ `Monday|Mon 10:00 AM~Haircut|Tuesday|Tue 10:00 AM~Haircut` ❌ `Monday|10:00 AM~Haircut|Tuesday|10:00 AM~Haircut`

## When the user answers

A tap on a button or list row reaches you as a tagged message, not plain text: `[Button reply: <title>]` or `[List reply: <title> — <description>]`. Treat it as the user's answer to the last interactive message you sent, and act on the title.
