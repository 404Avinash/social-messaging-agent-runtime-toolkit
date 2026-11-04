# AGENTS.md — generate-image recipe

AI-optimized wiring guide. Goal: generate an image from text and send it via WhatsApp.

## Files copied by `socialagent recipe add generate-image`

| Source | Destination | Purpose |
|--------|-------------|---------|
| `tool.py` | `src/tools/generate_image.py` | `generate_image(prompt, width, height)` → Bedrock SD 3.5 (`IMAGE_MODEL_REGION`, default us-west-2) → S3 → WhatsApp media id |
| `skill.md` | `src/skills/generate_image.md` | Agent guidance on when/how to generate images |

## Wiring steps

1. Import `generate_image` from the copied tool module into `src/tools/__init__.py`.
2. Define a Strands tool (e.g. `generate_image_tool`) with `@tool(context=True)`.
3. `await generate_image(prompt)` (it is async). If the result has `"error"`, `raise ValueError(result["error"])`.
4. Send the image with the `payload` wrapper (required — `notify.send_message` reads `message["payload"]`). Use `image_id` when present, else the presigned-URL fallback:
   `image = {"id": result["image_id"]} if "image_id" in result else {"link": result["image_url"]}` then `await notify.send_message(phone, {"type": "image", "payload": {"image": image}})` using `tool_context.invocation_state.get("phone", "")`.
5. Run inside `socialagent_notify_context()` so the outbound image is queued and drained by the webhook after the turn.

## Required env vars

| Variable | Required | Purpose |
|----------|----------|---------|
| `MEDIA_BUCKET` | yes | S3 bucket for the JPEG (falls back to `CONVERSATION_LOGS_BUCKET`) |
| `IMAGE_MODEL_REGION` | no | Region of the Bedrock call (default `us-west-2`; empty counts as unset). Set under `agent.environment` in `socialagent.<env>.yaml`; the model must be available there |
| `AWS_REGION` | no | Region for the S3 client (default `us-east-1`); Bedrock uses `IMAGE_MODEL_REGION` |
| `MEDIA_PREFIX` | no | Optional key prefix: `<prefix>/images/<uuid>.jpg` |

## Required IAM actions

Agent runtime needs:

- `bedrock:InvokeModel` (Stability SD 3.5 in `IMAGE_MODEL_REGION`, default `us-west-2`). The stack's runtime role already allows foundation models in every Region, so another Region needs no IAM change
- `s3:PutObject` (on `MEDIA_BUCKET`)
- `s3:GetObject` (only for the presigned-URL fallback)

The S3 → WhatsApp upload runs in the notification Lambda via `notify.upload_media`, so the agent runtime needs **no** `socialmessaging` permission (only `src/socialagent/providers/eum_social.py`, inside the notification Lambda, calls `socialmessaging`).

## Notes

- Bedrock model `stability.sd3-5-large-v1:0` is not offered in every Region, so the tool calls it in `IMAGE_MODEL_REGION` (read at call time; unset or empty means `us-west-2`), regardless of `AWS_REGION`. It calls the model ID directly, not an inference profile, so the request stays in that Region; a Region without the model makes the call fail.
- `socialagent recipe add` never overwrites, so a project copy taken before `IMAGE_MODEL_REGION` existed ignores it and still calls `us-west-2`.
- Generation takes ~5–15 seconds; the skill instructs the agent to tell the user it is generating.
- In dev mode (`socialagent dev`), `generate_image` skips Bedrock/S3 and returns `{"image_id": "dev-media-id"}`.
