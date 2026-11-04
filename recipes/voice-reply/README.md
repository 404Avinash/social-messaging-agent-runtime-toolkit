# voice-reply

Reply to users with a voice note synthesized from text via Amazon Polly.

## Overview

The `voice-reply` recipe ships `text_to_voice(text, language="es-MX", voice_id="")`, which converts text to speech with Amazon Polly (MP3 output — WhatsApp rejects OGG), uploads the audio to S3, then registers it with WhatsApp through `socialagent_runtime.notify.upload_media(...)` (which routes to the notification Lambda). It returns `{"audio_id": "<whatsapp_media_id>"}` on success or `{"error": "..."}` on failure. Your tool wrapper then sends the resulting media id to the user as an audio message.

`voice_id` defaults to a voice that matches `language` (e.g. `Mia` for es-MX, `Joanna` for en-US), so passing only `language` never causes a voice/language mismatch.

The accompanying skill tells the agent when a voice reply is appropriate (e.g. the user asks for audio, pronunciation help, short conversational replies) and when to stay with text (long or structured content).

## Files

`socialagent recipe add voice-reply` copies these files into the enclosing project (the nearest directory, from the current one upward, with an `agent.py` or a `socialagent.<env>.yaml`; the current directory if there is none):

| Source | Destination |
|--------|-------------|
| `tool.py` | `src/tools/voice_reply.py` |
| `skill.md` | `src/skills/voice_reply.md` |

`tool.py` exports `text_to_voice(...)`. `skill.md` is the agent-facing guidance.

## Wiring

Wrap `text_to_voice` as a Strands tool (e.g. `voice_reply`) in your project's `src/tools/__init__.py`, then send the returned media id to the user as an audio message. Note the `payload` wrapper — `notify.send_message` reads the WhatsApp object from `message["payload"]`, so a message without it is delivered empty:

```python
from strands import tool, ToolContext
from src.tools.voice_reply import text_to_voice
import socialagent_runtime.notify as notify

@tool(context=True)
async def voice_reply(text: str, tool_context: ToolContext, language: str = "es-MX") -> dict:
    result = await text_to_voice(text, language=language)
    if "error" in result:
        raise ValueError(result["error"])
    phone = tool_context.invocation_state.get("phone", "")
    await notify.send_message(
        phone,
        {"type": "audio", "payload": {"audio": {"id": result["audio_id"]}}},
    )
    return {"sent": True}
```

Run the tool inside `socialagent_notify_context()` (the standard `agent.py` entrypoint) so the outbound audio is queued and drained by the webhook after the turn. A queued send goes to the current user and ignores `log_to_memory` and `memory_text`, so the example passes neither; for the agent to remember the voice note on later turns, have it mention the note in its reply.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `MEDIA_BUCKET` | yes | S3 bucket the synthesized MP3 is written to (falls back to `CONVERSATION_LOGS_BUCKET` if unset) |
| `AWS_REGION` | no | Region for Polly and S3 clients (defaults to `us-east-1`) |
| `MEDIA_PREFIX` | no | Optional key prefix for uploaded audio (`<prefix>/voice/<uuid>.mp3`) |

## IAM

The agent runtime executing this tool needs:

- `polly:SynthesizeSpeech` — text-to-speech synthesis
- `s3:PutObject` — write the MP3 to `MEDIA_BUCKET`

The WhatsApp media upload is performed by the **notification Lambda** (via `notify.upload_media` → `provider.upload_media_from_s3`), so the agent runtime does **not** need any `socialmessaging` permission — only the notification Lambda calls `socialmessaging`.
