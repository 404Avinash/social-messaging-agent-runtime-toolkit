# generate-image

Generate images from text with Amazon Bedrock (Stability SD 3.5) and send them via WhatsApp.

## Overview

The `generate-image` recipe ships `generate_image(prompt, width=1024, height=1024)`, which invokes Stability SD 3.5 through Amazon Bedrock in the Region `IMAGE_MODEL_REGION` names (`us-west-2` by default; see [Region](#region)), decodes the returned JPEG, uploads it to S3, then registers it with WhatsApp through `socialagent_runtime.notify.upload_media(...)` (which routes to the notification Lambda). On success it returns `{"image_id": "<whatsapp_media_id>", "prompt": ...}`; if the media upload returns nothing it falls back to `{"image_url": "<presigned_s3_url>", "prompt": ...}`; on failure it returns `{"error": "...", "prompt": ...}`.

The accompanying skill tells the agent when to generate an image (explicit visual requests), to write prompts in English, and to warn the user that generation takes several seconds.

## Files

`socialagent recipe add generate-image` copies these files into the enclosing project (the nearest directory, from the current one upward, with an `agent.py` or a `socialagent.<env>.yaml`; the current directory if there is none):

| Source | Destination |
|--------|-------------|
| `tool.py` | `src/tools/generate_image.py` |
| `skill.md` | `src/skills/generate_image.md` |

`tool.py` exports `generate_image(...)`. `skill.md` is the agent-facing guidance (named `image_generation` in its frontmatter).

## Wiring

Wrap `generate_image` as a Strands tool (e.g. `generate_image_tool`) in your project's `src/tools/__init__.py`. Note the `payload` wrapper — `notify.send_message` reads the WhatsApp object from `message["payload"]`, so a message without it is delivered empty:

```python
from strands import tool, ToolContext
from src.tools.generate_image import generate_image
import socialagent_runtime.notify as notify

@tool(context=True)
async def generate_image_tool(prompt: str, tool_context: ToolContext) -> dict:
    result = await generate_image(prompt)
    if "error" in result:
        raise ValueError(result["error"])
    phone = tool_context.invocation_state.get("phone", "")
    image = {"id": result["image_id"]} if "image_id" in result else {"link": result["image_url"]}
    image["caption"] = prompt
    await notify.send_message(phone, {"type": "image", "payload": {"image": image}})
    return {"sent": True}
```

Run the tool inside `socialagent_notify_context()` (the standard `agent.py` entrypoint) so the outbound image is queued and drained by the webhook after the turn.

## Region

The tool calls Bedrock in one Region, not necessarily the stack's: the image model is not offered in every Region. With `IMAGE_MODEL_REGION` unset or empty, the default keeps the request in `us-west-2`. To choose another Region, set it under `agent.environment` in `socialagent.<env>.yaml` and deploy:

```yaml
agent:
  environment:
    IMAGE_MODEL_REGION: <region>
```

The image model, `stability.sd3-5-large-v1:0`, must be available in that Region: the tool calls it by model ID, not through an inference profile, so the request runs in the Region named and nowhere else. Check the model's page in [models at a glance](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html). In a Region without the model, the call fails and the tool returns an error. Only the Bedrock call moves: the image is still stored in the stack's media bucket.

`socialagent recipe add` never overwrites an existing file, so a project whose `src/tools/generate_image.py` was copied before this setting existed keeps calling `us-west-2` and ignores the variable. Check that the project's copy reads `IMAGE_MODEL_REGION`. [Data Flows and Regions](../../docs/data-flows.md#image-generation) lists this call with the stack's other calls that can process content outside its Region.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `MEDIA_BUCKET` | yes | S3 bucket the generated JPEG is written to (falls back to `CONVERSATION_LOGS_BUCKET` if unset) |
| `IMAGE_MODEL_REGION` | no | Region of the Bedrock call (defaults to `us-west-2`; empty counts as unset). Set it under `agent.environment`; the model must be available there. See [Region](#region) |
| `AWS_REGION` | no | Region for the S3 client (defaults to `us-east-1`); Bedrock is called in `IMAGE_MODEL_REGION` |
| `MEDIA_PREFIX` | no | Optional key prefix for uploaded images (`<prefix>/images/<uuid>.jpg`) |

## IAM

The agent runtime executing this tool needs:

- `bedrock:InvokeModel` — invoke Stability SD 3.5 in `IMAGE_MODEL_REGION` (`us-west-2` by default). The stack's runtime role allows it on foundation models in every Region, so choosing another Region needs no IAM change
- `s3:PutObject` — write the JPEG to `MEDIA_BUCKET`
- `s3:GetObject` — only needed for the presigned-URL fallback (`generate_presigned_url`)

The WhatsApp media upload is performed by the **notification Lambda** (via `notify.upload_media` → `provider.upload_media_from_s3`), so the agent runtime does **not** need any `socialmessaging` permission — only the notification Lambda calls `socialmessaging`.
