# AGENTS.md — voice-reply recipe

AI-optimized wiring guide. Goal: reply with a Polly-synthesized WhatsApp voice note.

## Files copied by `socialagent recipe add voice-reply`

| Source | Destination | Purpose |
|--------|-------------|---------|
| `tool.py` | `src/tools/voice_reply.py` | `text_to_voice(text, language, voice_id)` → Polly TTS → S3 → WhatsApp media id |
| `skill.md` | `src/skills/voice_reply.md` | Agent guidance on when to reply with audio vs text |

## Wiring steps

1. Import `text_to_voice` from the copied tool module into `src/tools/__init__.py`.
2. Define a Strands tool (e.g. `voice_reply`) with `@tool(context=True)`.
3. `await text_to_voice(text, language=...)` (it is async). If the result has `"error"`, `raise ValueError(result["error"])`.
4. Send the audio with the `payload` wrapper (required — `notify.send_message` reads `message["payload"]`):
   `await notify.send_message(phone, {"type": "audio", "payload": {"audio": {"id": result["audio_id"]}}})` using `tool_context.invocation_state.get("phone", "")`. Pass no `log_to_memory` or `memory_text`: the send is queued (step 5), and a queued send ignores both.
5. Run inside `socialagent_notify_context()` so the outbound audio is queued and drained by the webhook after the turn.

## Required env vars

| Variable | Required | Purpose |
|----------|----------|---------|
| `MEDIA_BUCKET` | yes | S3 bucket for the MP3 (falls back to `CONVERSATION_LOGS_BUCKET`) |
| `AWS_REGION` | no | Region for Polly/S3 clients (default `us-east-1`) |
| `MEDIA_PREFIX` | no | Optional key prefix: `<prefix>/voice/<uuid>.mp3` |

## Required IAM actions

Agent runtime only needs:

- `polly:SynthesizeSpeech`
- `s3:PutObject` (on `MEDIA_BUCKET`)

The S3 → WhatsApp upload runs in the notification Lambda via `notify.upload_media`, so the agent runtime needs **no** `socialmessaging` permission (only `src/socialagent/providers/eum_social.py`, inside the notification Lambda, calls `socialmessaging`).

## Notes

- Output must be MP3 — WhatsApp rejects OGG.
- `voice_id` defaults per language (Mia for es-MX, Joanna for en-US); only override it with a voice that supports the chosen `language`, or Polly rejects the call.
- Polly neural voices for `es-MX` may be unavailable in some regions; verify the chosen `voice_id` before relying on it.
- In dev mode (`socialagent dev`), `text_to_voice` skips Polly/S3 and returns `{"audio_id": "dev-media-id"}`.
