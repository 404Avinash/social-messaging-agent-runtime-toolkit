---
name: scheduling
description: Schedule reminders, tasks, and recurring operations at specific times or intervals
operations:
  - name: reminder
    description: Send the same text message each time
    tool: schedule_recurring
  - name: invoke_agent
    description: Perform the message as a task each time and send the result
    tool: schedule_recurring
---

# Scheduling

You have five scheduling tools. Pick the right one based on what the user asks.

## Tools

### schedule_reminder(message, delay_minutes, at_datetime, timezone) — Send a message later
- "Remind me in 30 minutes to call mom" → `message="Call mom!", delay_minutes=30`
- "Remind me tomorrow at 9am to pick up my package" (user in Mexico City) → `message="Pick up your package!", at_datetime="2026-04-16T09:00:00", timezone="America/Mexico_City"`
- Just sends a text message, no agent processing

### schedule_task(task, delay_minutes, at_datetime, timezone) — You do something later
- "In 1 hour write me a short stretching routine" → `task="Write a short stretching routine", delay_minutes=60`
- "Tomorrow at 8am write me a motivational quote" → `task="Write a short motivational quote", at_datetime="2026-04-16T08:00:00", timezone="America/Mexico_City"`
- "Tonight at 9 send me my shopping list" → `task="Recall my shopping list and send it to me", at_datetime="2026-04-15T21:00:00", timezone="America/Mexico_City"`
- Re-invokes you with the task prompt. The task has the same tools you have now and nothing else, and it cannot create schedules: `schedule_reminder`, `schedule_task` and `schedule_recurring` refuse inside it. So don't write a task that schedules something ("...then remind me again tomorrow"): create that schedule now, alongside the task
- A task fires at least 15 minutes from now: `delay_minutes` of 15 or more, or an `at_datetime` at least 15 minutes away. For anything sooner, do it now in your reply, or schedule a reminder, which can fire after any delay
- The task answers in text only. A voice note, image or interactive message it sends (`voice_reply`, `generate_image_tool`, `attach_interactive`) is never delivered, so don't schedule a task that needs one: for "in 1 hour send me a voice note", say a scheduled task can't send voice notes and offer a text task or a reminder instead
- The result arrives as a short notice with a preview; a result too long for the preview, or with line breaks, then follows in full. Outside the user's 24-hour WhatsApp window only the notice arrives; if the user says they only got part of it, send the full result from the conversation

### schedule_recurring(operation, cron, message, timezone) — Repeat on a schedule
- "Every day at 9am remind me to drink water" → `operation="reminder", cron="0 9 * * ? *", message="Drink water!", timezone="America/Mexico_City"`
- "Every Monday at 8am remind me to submit my report" → `operation="reminder", cron="0 8 ? * MON *", message="Submit your weekly report!", timezone="America/Mexico_City"`
- "Every morning at 7 write me a motivational quote" → `operation="invoke_agent", cron="0 7 * * ? *", message="Write a short motivational quote", timezone="America/Mexico_City"`
- `operation` is `"reminder"` to send the same text each time, or `"invoke_agent"` to perform `message` as a task each time (text only, as with `schedule_task`). No other operation name works
- Keeps firing until cancelled, and fires at most once every 15 minutes
- The cron's year field is always `*`: a cron with a year is refused. For one date and time, use `schedule_reminder` or `schedule_task`

### list_schedules() — See active schedules
- "What reminders do I have?" → call `list_schedules()`
- Returns each schedule's name, operation, message, expression and time zone. It does not say when a schedule fires next: describe the timing from the expression and time zone, and never make up a next fire time

### cancel_schedule(schedule_id) — Cancel a schedule
- "Cancel my daily water reminder" → first call `list_schedules()` to get the ID, then `cancel_schedule(schedule_id="...")`
- Always list schedules first to get the correct ID

## Rules

1. **Ask for timezone** if the user says a specific time but you don't know their timezone. Check `recall(key="timezone")` first, and store a new one with `remember(key="timezone", value="America/Mexico_City")` for future use.
2. **Use `at_datetime`** when the user says a specific date/time. Write it in the user's local time with no UTC offset (e.g. `2026-04-16T09:00:00`) and pass their IANA name as `timezone`: the reminder then fires at that local time, whatever offset the zone has on that date. A time with no offset and no `timezone` is refused.
3. **Use `delay_minutes`** when the user says "in X minutes/hours". Convert hours to minutes.
4. **Write recurring crons in the user's local time** and pass their IANA name as `timezone`. The schedule then keeps the same local hour when the zone's offset changes. Without `timezone` the cron is read in UTC.
5. **Write messages and tasks in the user's language** — the scheduled agent will respond in that language.
6. **To cancel**, always call `list_schedules()` first to find the schedule ID.
7. **To change a schedule** (its time, text or time zone), call `list_schedules()`, `cancel_schedule` the old one, then create the new one. Scheduling it again at the new time adds a second schedule, and both keep firing.
8. **Decline live or external data.** You have no tool for weather, news, prices, exchange rates, sports scores or web search, and neither does a scheduled task. Don't schedule a task or reminder that promises such data, and never make it up: tell the user you can't fetch it, and offer a plain reminder instead (e.g. "check the weather before you leave").

## CRON Format (EventBridge)

```
cron(minutes hours day-of-month month day-of-week year)
```

Examples, read in the `timezone` you pass (UTC without one):
- Daily at 3pm: `0 15 * * ? *`
- Weekdays at 2pm: `0 14 ? * MON-FRI *`
- Every Monday at 9am: `0 9 ? * MON *`
- First of every month at noon: `0 12 1 * ? *`
- Every 30 minutes: `0/30 * * * ? *`

One of day-of-month and day-of-week must be `?`.
