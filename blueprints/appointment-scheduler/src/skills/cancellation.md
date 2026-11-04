---
name: cancellation
description: Cancel an existing booking
---

Cancel the user's active visit booking.

## Rules
1. Read the booking from the context injected into your system prompt
2. If no booking exists: say so and offer to schedule one
3. Call execute_action("cancel_visit", {}) as soon as the user asks to cancel. The tool asks for confirmation itself: it sends the booking details with its own buttons (`Yes, cancel` / `No, keep it`, or `Sí, cancelar` / `No, mantener` in Spanish) and returns `confirmation_requested`; then output exactly one short line, never nothing and never the question again. Examples: "Tap 'Yes, cancel' to confirm." — "Toca 'Sí, cancelar' para confirmar." While that question is open, ask the user nothing else, so their next message can only be an answer to the card. Call execute_action("cancel_visit", {}) again only on the user's reply to the card, once per message; the tool judges the answer. A reply to any other question is not a cancel answer and never goes to the tool. A tap on `Yes, cancel`, or a typed message that is nothing but a short yes ("yes", "sí, cancélala por favor"), cancels and returns `cancelled`; a no (a tap on `No, keep it`, "no", "keep it", a typed 👎) returns `declined` and you keep the booking; anything else sends the buttons again and returns `confirmation_requested`. After `cancelled` do not call it again in the same turn: the booking is gone. Never send the confirmation yourself with attach_interactive; never cancel another way.
4. After cancellation: confirm it's done and offer to reschedule
