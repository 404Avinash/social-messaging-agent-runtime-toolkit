---
name: voice_reply
description: Reply with voice messages when audio is more appropriate than text
---

# Voice Reply

You have a `voice_reply` tool that converts text to a WhatsApp voice note.

## When to Use
- User says "dime en audio", "mándame un audio", "quiero escucharte"
- Pronunciation help: "how do you pronounce...?"
- Storytelling or reading aloud
- When the response is short and conversational

## When NOT to Use
- Long responses (>500 chars) — voice notes get tedious
- Lists, tables, or structured data — text is better
- When the user is clearly reading (not listening)

## Rules
- Keep voice messages SHORT (1-2 paragraphs max)
- The text you pass becomes the audio — write naturally, not formally
- Pass `language` for the language you write the text in, which should be the user's: `es-MX` (the default), `es-US`, `en-US` or `en-GB`. The tool does not detect it, so English text without `language="en-US"` is read by a Spanish voice
- There is no voice for any other language: reply in text instead
