---
name: status_check
description: Show the user their current booking status
---

Check and display the user's current booking.

## Rules
1. Read the booking from the context injected into your system prompt
2. If a booking exists: show the listing, date, time, and address clearly
3. If no booking: say so and offer to help schedule one
4. Do not call any tools — all info is already in your context
