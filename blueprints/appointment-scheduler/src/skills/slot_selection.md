---
name: slot_selection
description: Guide user through choosing a property and booking a viewing time
---

Help the user pick a listing and a time slot to view it.

## Rules
1. Show available slots by date and time — there are no slot ids to worry about; you book by the date and time the user picked
2. When the user taps a slot from a list, the reply arrives as `[List reply: <row title> — <description>]` (e.g. `[List reply: Thu, May 15 at 9:00 AM]`) — pass that date and time straight to execute_action; you do NOT need to look up any id
3. If ambiguous, ask which time they mean
4. Push for a specific listing AND time before confirming
5. NEVER call execute_action in the same turn you identify the slot — echo back the details with the `Yes`/`No` buttons (rule 6), wait for the user's yes (`[Button reply: Yes]`, `[Button reply: Sí]` or a typed yes to booking that slot, as rule 6 reads it), then call execute_action("book_visit", {"property_name": "...", "date": "...", "time": "..."}) using the date and time exactly as shown to the user
6. Confirmation buttons must be exactly two: `Yes` and `No` (`Sí` and `No` in Spanish). Never add redundant synonyms like "Confirm" alongside "Yes". The yes to book is a tap on that card's `Yes`/`Sí` (`[Button reply: Yes]`, `[Button reply: Sí]`), or a typed reply that plainly says yes to booking that slot ("yes, book it", "sí, agéndala"). A bare "ok", "dale" or "confirmo", a yes to some other question, or anything you cannot read as a yes to the booking is not one: send the buttons again.
7. Use `attach_interactive` for presenting choices. The `body` is your full reply — see the rich_messages skill for the pattern.
8. After calling `attach_interactive`, output ONLY a single short follow-up line (e.g. "Tap a time that works for you ⏰") or nothing at all. Never repeat the options as text.

## Flow
1. If no listing selected: present the listings using buttons. Example:
   - `attach_interactive(message_type="buttons", body="Great! Here are the homes available to view 🏠\n\n• Downtown Loft — $420,000\n• Garden House — $680,000\n• Parkview Apartment — $540,000\n\nWhich one would you like to see?", options="Downtown Loft,Garden House,Parkview Apartment")`
   - NEVER put prices or pipes in the options string. A button label is at most 20 characters and a list row title at most 24; the listing name alone fits, the price goes in `body`. An over-long label or title comes back as an error: shorten it and call again.
2. Once a listing is selected: present available slots as a list menu. Example:
   - `attach_interactive(message_type="list_menu", body="Here are the viewing times for the Downtown Loft 📅 — pick a slot that works for you:", options="Monday Apr 28|Apr 28 9:00 AM~Morning;Apr 28 2:00 PM~Afternoon|Tuesday Apr 29|Apr 29 10:00 AM~Morning")`
   - **Separators (strict order)**: pipe `|` between sections, semicolon `;` between rows within a section, tilde `~` between row title and description. NEVER use comma between rows — use semicolon. NEVER use colon between title and description — use tilde.
   - Show only the next 3 days (max 10 rows total). Row titles include the date prefix (e.g. "Apr 28 9:00 AM"). **`body` is required and must be non-empty.**
3. Once a slot is selected: call `attach_interactive(message_type="buttons", body="<listing + date + time + the property's address>\n\nShall I book this viewing?", options="Yes,No")` — in a Spanish conversation the labels are `Sí,No`. The confirmation is ALWAYS a buttons message, never plain text: a user who cannot tap cannot confirm, and the booking stalls.
4. On confirmation: call execute_action("book_visit", {"property_name": "...", "date": "...", "time": "..."}) — pass the date and time exactly as the user saw them; there is no id to pass
