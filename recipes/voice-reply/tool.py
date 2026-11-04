# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Voice reply — Amazon Polly TTS, uploads to WhatsApp via the notification Lambda."""

from __future__ import annotations

import asyncio
import os
import uuid

import boto3

from socialagent_runtime import notify
from socialagent_runtime.config import config

# Default Polly voice per language so a caller passing only ``language`` never
# hits a voice/language mismatch (e.g. the es-MX voice "Mia" with en-US text).
_DEFAULT_VOICES = {
    "es-MX": "Mia",
    "es-US": "Lupe",
    "en-US": "Joanna",
    "en-GB": "Amy",
}


async def text_to_voice(text: str, language: str = "es-MX", voice_id: str = "") -> dict:
    """Convert text to speech and upload to WhatsApp. Returns a media ID for sending.

    Args:
        text: Text to convert to speech.
        language: Polly language code (es-MX, en-US, ...).
        voice_id: Polly voice ID. Defaults to a voice that matches ``language``
            (Mia for es-MX, Joanna for en-US). Override only if you know the
            voice supports the language, or Polly will reject the request.

    Returns:
        ``{"audio_id": "<whatsapp_media_id>"}`` on success, ``{"error": "..."}``
        on failure.
    """
    voice = voice_id or _DEFAULT_VOICES.get(language, "Mia")

    # Dev mode: skip Polly/S3/AWS entirely and return the stub media id that
    # notify.upload_media also returns, so `socialagent dev` exercises the full path.
    if config.is_dev_mode:
        print(
            f"[dev] text_to_voice: voice={voice} language={language} text={text[:60]!r}"
        )
        return {"audio_id": "dev-media-id"}

    bucket = config.media_bucket
    if not bucket:
        return {"error": "No S3 bucket configured for media uploads (set MEDIA_BUCKET)"}

    try:
        region = config.region
        # Generate MP3 — WhatsApp rejects OGG.
        polly = boto3.client("polly", region_name=region)
        resp = await asyncio.to_thread(
            polly.synthesize_speech,
            Text=text[:3000],
            OutputFormat="mp3",
            VoiceId=voice,
            LanguageCode=language,
        )
        audio_bytes = resp["AudioStream"].read()

        # Upload to S3.
        s3 = boto3.client("s3", region_name=region)
        prefix = os.environ.get("MEDIA_PREFIX", "").rstrip("/")
        key = (
            f"{prefix}/voice/{uuid.uuid4()}.mp3"
            if prefix
            else f"voice/{uuid.uuid4()}.mp3"
        )
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket,
            Key=key,
            Body=audio_bytes,
            ContentType="audio/mpeg",
        )

        # Upload to WhatsApp via the notification Lambda (provider-agnostic).
        media_id = await notify.upload_media(bucket, key)
        if not media_id:
            return {"error": "Media upload returned no mediaId"}

        return {"audio_id": media_id}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
