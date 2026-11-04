# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Image generation — Stability SD 3.5 via Bedrock, in IMAGE_MODEL_REGION."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid

import boto3

from socialagent_runtime import notify
from socialagent_runtime.config import config

# Stability SD 3.5 is not offered in every Region, so the model is called in a
# Region of its own, not the stack's: IMAGE_MODEL_REGION, read at call time like
# MEDIA_PREFIX, or us-west-2 when it is unset or empty. The model must be
# available in the Region chosen.
_DEFAULT_BEDROCK_REGION = "us-west-2"
_MODEL_ID = "stability.sd3-5-large-v1:0"


async def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> dict:
    """Generate an image from a text description. Returns a WhatsApp media id.

    Calls Stability SD 3.5 in ``IMAGE_MODEL_REGION`` (default ``us-west-2``).

    Args:
        prompt: Description of the image to generate.
        width: Image width hint (used to pick aspect ratio).
        height: Image height hint (used to pick aspect ratio).

    Returns:
        ``{"image_id": "<whatsapp_media_id>", "prompt": ...}`` on success,
        or ``{"image_url": "<presigned_url>", "prompt": ...}`` if the WhatsApp
        media upload returned nothing, or ``{"error": ..., "prompt": ...}`` on
        failure.
    """
    # text-to-image aspect ratio: portrait, landscape, or square based on the hint.
    if width > height:
        aspect_ratio = "16:9"
    elif height > width:
        aspect_ratio = "9:16"
    else:
        aspect_ratio = "1:1"

    # Dev mode: skip Bedrock/S3/AWS entirely and return the stub media id that
    # notify.upload_media also returns, so `socialagent dev` exercises the full path.
    if config.is_dev_mode:
        print(f"[dev] generate_image: aspect={aspect_ratio} prompt={prompt[:60]!r}")
        return {"image_id": "dev-media-id", "prompt": prompt}

    bucket = config.media_bucket
    if not bucket:
        return {
            "error": "No S3 bucket configured for media uploads (set MEDIA_BUCKET)",
            "prompt": prompt,
        }

    try:
        bedrock_region = os.environ.get("IMAGE_MODEL_REGION") or _DEFAULT_BEDROCK_REGION
        bedrock = boto3.client("bedrock-runtime", region_name=bedrock_region)
        body = json.dumps(
            {
                "prompt": prompt,
                "mode": "text-to-image",
                "output_format": "jpeg",
                "aspect_ratio": aspect_ratio,
            }
        )
        resp = await asyncio.to_thread(
            bedrock.invoke_model,
            modelId=_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(resp["body"].read())
        # Stability signals a content-filter block in finish_reasons, not as an
        # error, and may still return a blank/absent image. Decoding it anyway
        # uploaded an empty image that WhatsApp then rejected or showed blank —
        # surface it as an error instead.
        finish_reasons = result.get("finish_reasons") or []
        if finish_reasons and finish_reasons[0]:
            return {
                "error": f"Image generation was filtered: {finish_reasons[0]}",
                "prompt": prompt,
            }
        images = result.get("images") or []
        if not images or not images[0]:
            return {"error": "Image generation returned no image", "prompt": prompt}
        image_bytes = base64.b64decode(images[0])

        # Upload to S3.
        region = config.region
        s3 = boto3.client("s3", region_name=region)
        prefix = os.environ.get("MEDIA_PREFIX", "").rstrip("/")
        key = (
            f"{prefix}/images/{uuid.uuid4()}.jpg"
            if prefix
            else f"images/{uuid.uuid4()}.jpg"
        )
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket,
            Key=key,
            Body=image_bytes,
            ContentType="image/jpeg",
        )

        # Upload to WhatsApp via the notification Lambda (provider-agnostic).
        media_id = await notify.upload_media(bucket, key)
        if media_id:
            return {"image_id": media_id, "prompt": prompt}

        # Fallback to a presigned URL if no media id was returned. WhatsApp
        # cannot reliably fetch presigned S3 URLs, so this is a last resort.
        url = await asyncio.to_thread(
            s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
        return {"image_url": url, "prompt": prompt}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "prompt": prompt}
