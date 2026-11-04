# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Audio transcription via Amazon Transcribe.

Flow:
1. Upload audio bytes to a temp S3 key under ``TRANSCRIBE_S3_PREFIX``.
2. Start a transcription job with a unique name.
3. Poll until COMPLETED or FAILED (up to ``TRANSCRIBE_TIMEOUT`` seconds).
4. Fetch the transcript JSON from the result URI and extract the text.
5. Clean up the temp S3 object and the transcription job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import uuid

import boto3

from socialagent.constants import (
    TRANSCRIBE_POLL_INTERVAL,
    TRANSCRIBE_S3_PREFIX,
    TRANSCRIBE_TIMEOUT,
)

logger = logging.getLogger(__name__)

_MIME_TO_FORMAT: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "mp4",
    "audio/aac": "mp4",
    "audio/x-m4a": "mp4",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def _media_format(mime_type: str) -> str:
    """Return the Transcribe MediaFormat string for a given MIME type.

    Falls back to ``"mp4"`` for unknown types.
    """
    return _MIME_TO_FORMAT.get(mime_type.lower().split(";")[0].strip(), "mp4")


def _do_transcribe(
    audio_bytes: bytes,
    mime_type: str,
    bucket: str,
    region: str,
    poll_interval: int,
    timeout: int,
    key_prefix: str = "",
    language_options: tuple[str, ...] = (),
) -> str:
    """Run the full transcription flow synchronously (called via asyncio.to_thread).

    Raises :class:`RuntimeError` on job failure or timeout.
    """
    job_id = f"socialagent-{uuid.uuid4().hex}"
    # Build S3 key: optionally scope under a stack prefix for IAM isolation
    # in multi-stack deployments sharing a single bucket.
    base_key = f"{TRANSCRIBE_S3_PREFIX}{job_id}"
    s3_key = f"{key_prefix.rstrip('/')}/{base_key}" if key_prefix else base_key
    media_format = _media_format(mime_type)

    s3 = boto3.client("s3", region_name=region)
    transcribe = boto3.client("transcribe", region_name=region)

    # 1. Upload
    s3.put_object(Bucket=bucket, Key=s3_key, Body=audio_bytes)
    media_uri = f"s3://{bucket}/{s3_key}"

    try:
        # 2. Start job
        # Identification among every language misreads a one-word note (a
        # Spanish "ve" came back as English "Go"); LanguageOptions limits the
        # guess to the languages the bot serves. Transcribe needs two or more.
        job_args = {
            "TranscriptionJobName": job_id,
            "Media": {"MediaFileUri": media_uri},
            "MediaFormat": media_format,
            "IdentifyLanguage": True,
        }
        if len(language_options) >= 2:
            job_args["LanguageOptions"] = list(language_options)
        elif len(language_options) == 1:
            job_args = {
                **job_args,
                "IdentifyLanguage": False,
                "LanguageCode": language_options[0],
            }
        transcribe.start_transcription_job(**job_args)

        # 3. Poll
        deadline = time.monotonic() + timeout
        while True:
            resp = transcribe.get_transcription_job(TranscriptionJobName=job_id)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED":
                reason = resp["TranscriptionJob"].get("FailureReason", "unknown")
                raise RuntimeError(f"Transcription job failed: {reason}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"Transcription job timed out after {timeout}s")
            time.sleep(poll_interval)

        # 4. Fetch transcript
        transcript_uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        with urllib.request.urlopen(transcript_uri) as f:  # noqa: S310
            data = json.loads(f.read())
        transcript = data["results"]["transcripts"][0]["transcript"]
        # Length and language, never the words: enough to see a misread note
        # in the log without writing what the user said into it.
        logger.info(
            "transcribed voice note: language=%s chars=%d",
            resp["TranscriptionJob"].get("LanguageCode", "?"),
            len(transcript),
        )
        return transcript

    finally:
        # 5. Cleanup — best-effort, never raises
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to delete temp S3 object: s3://%s/%s", bucket, s3_key
            )
        try:
            transcribe.delete_transcription_job(TranscriptionJobName=job_id)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to delete transcription job: %s", job_id)


async def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    bucket: str,
    region: str = "us-east-1",
    poll_interval: int = TRANSCRIBE_POLL_INTERVAL,
    max_wait: int = TRANSCRIBE_TIMEOUT,
    key_prefix: str = "",
    language_options: tuple[str, ...] = (),
) -> str:
    """Transcribe audio bytes to text using Amazon Transcribe.

    Uploads the audio to a temporary S3 key, starts a transcription job,
    polls until completion, fetches the result, and cleans up.

    Args:
        audio_bytes: Raw audio data.
        mime_type: MIME type of the audio (e.g. ``"audio/ogg"``).
        bucket: S3 bucket name for temporary storage.
        region: AWS region.
        poll_interval: Seconds between status polls.
        max_wait: Maximum seconds to wait before raising.
        key_prefix: Optional S3 key prefix (e.g. ``"stacks/scheduler-dev"``).
            When set, the temp object is written at
            ``{key_prefix}/{TRANSCRIBE_S3_PREFIX}{job_id}`` so IAM can scope
            Transcribe S3 access per stack on a shared bucket.

    Returns:
        Transcribed text string.

    Raises:
        RuntimeError: If the job fails or times out.
    """
    return await asyncio.to_thread(
        _do_transcribe,
        audio_bytes,
        mime_type,
        bucket,
        region,
        poll_interval,
        max_wait,
        key_prefix,
        language_options,
    )
