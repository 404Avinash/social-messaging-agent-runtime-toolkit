# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for transcribe_audio and helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from socialagent.media.transcriber import (
    _media_format,
    _do_transcribe,
    transcribe_audio,
)


# ── _media_format ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("audio/ogg", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp3", "mp3"),
        ("audio/wav", "wav"),
        ("audio/x-wav", "wav"),
        ("audio/mp4", "mp4"),
        ("audio/aac", "mp4"),
        ("audio/x-m4a", "mp4"),
        ("audio/webm", "webm"),
        ("audio/flac", "flac"),
        ("audio/unknown", "mp4"),  # fallback
        ("audio/ogg; codecs=opus", "ogg"),  # strips params
    ],
)
def test_media_format(mime, expected):
    """_media_format maps MIME types to Transcribe MediaFormat strings."""
    assert _media_format(mime) == expected


# ── _do_transcribe ─────────────────────────────────────────────────────────────


def _make_clients(status_sequence: list[str], transcript: str = "Hola mundo"):
    """Build mock S3 and Transcribe clients for _do_transcribe tests."""
    s3 = MagicMock()
    transcribe = MagicMock()

    responses = [
        {
            "TranscriptionJob": {
                "TranscriptionJobStatus": s,
                "Transcript": {"TranscriptFileUri": "https://example.com/t.json"},
            }
        }
        for s in status_sequence
    ]
    transcribe.get_transcription_job.side_effect = responses

    transcript_json = json.dumps(
        {"results": {"transcripts": [{"transcript": transcript}]}}
    ).encode()

    return s3, transcribe, transcript_json


def test_do_transcribe_happy_path():
    """_do_transcribe uploads, starts job, polls, fetches transcript, cleans up."""
    s3, transcribe, transcript_json = _make_clients(["IN_PROGRESS", "COMPLETED"])

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.urllib.request.urlopen") as mock_open,
    ):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=transcript_json)

        result = _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=10
        )

    assert result == "Hola mundo"
    s3.put_object.assert_called_once()
    transcribe.start_transcription_job.assert_called_once()
    assert transcribe.get_transcription_job.call_count == 2
    s3.delete_object.assert_called_once()
    transcribe.delete_transcription_job.assert_called_once()


def test_do_transcribe_s3_key_uses_prefix():
    """Temp S3 key starts with TRANSCRIBE_S3_PREFIX."""
    s3, transcribe, transcript_json = _make_clients(["COMPLETED"])

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.urllib.request.urlopen") as mock_open,
    ):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=transcript_json)

        _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=10
        )

    key = s3.put_object.call_args.kwargs["Key"]
    assert key.startswith("socialagent-transcribe/")


def test_do_transcribe_s3_key_scoped_by_key_prefix():
    """When key_prefix is provided, the S3 key is written under it.

    Required for multi-stack shared-bucket deployments — IAM scopes each
    stack's webhook Lambda to its own stack prefix (e.g. stacks/foo-dev/),
    so the transcriber must write inside that prefix.
    """
    s3, transcribe, transcript_json = _make_clients(["COMPLETED"])

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.urllib.request.urlopen") as mock_open,
    ):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=transcript_json)

        _do_transcribe(
            b"audio",
            "audio/ogg",
            "my-bucket",
            "us-east-1",
            poll_interval=0,
            timeout=10,
            key_prefix="stacks/scheduler-dev",
        )

    key = s3.put_object.call_args.kwargs["Key"]
    assert key.startswith("stacks/scheduler-dev/socialagent-transcribe/")
    # Trailing slash on the prefix is also handled
    assert "//" not in key


def test_do_transcribe_raises_on_failed_job():
    """_do_transcribe raises RuntimeError when job status is FAILED."""
    s3 = MagicMock()
    transcribe = MagicMock()
    transcribe.get_transcription_job.return_value = {
        "TranscriptionJob": {
            "TranscriptionJobStatus": "FAILED",
            "FailureReason": "bad audio",
        }
    }

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        pytest.raises(RuntimeError, match="bad audio"),
    ):
        _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=10
        )


def test_do_transcribe_raises_on_timeout():
    """_do_transcribe raises RuntimeError when job does not complete in time."""
    s3 = MagicMock()
    transcribe = MagicMock()
    transcribe.get_transcription_job.return_value = {
        "TranscriptionJob": {
            "TranscriptionJobStatus": "IN_PROGRESS",
            "Transcript": {},
        }
    }

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.time.monotonic", side_effect=[0, 0, 999]),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=1
        )


def test_do_transcribe_cleanup_runs_even_on_failure():
    """S3 and job cleanup always runs, even when the job fails."""
    s3 = MagicMock()
    transcribe = MagicMock()
    transcribe.get_transcription_job.return_value = {
        "TranscriptionJob": {"TranscriptionJobStatus": "FAILED", "FailureReason": "err"}
    }

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        pytest.raises(RuntimeError),
    ):
        _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=10
        )

    s3.delete_object.assert_called_once()
    transcribe.delete_transcription_job.assert_called_once()


def test_do_transcribe_cleanup_failure_does_not_raise():
    """Cleanup errors are swallowed and do not propagate."""
    s3, transcribe, transcript_json = _make_clients(["COMPLETED"])
    s3.delete_object.side_effect = Exception("s3 error")
    transcribe.delete_transcription_job.side_effect = Exception("transcribe error")

    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.urllib.request.urlopen") as mock_open,
    ):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=transcript_json)

        result = _do_transcribe(
            b"audio", "audio/ogg", "my-bucket", "us-east-1", poll_interval=0, timeout=10
        )

    assert result == "Hola mundo"


# ── transcribe_audio (async wrapper) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcribe_audio_returns_text():
    """transcribe_audio delegates to _do_transcribe and returns its result."""
    with patch(
        "socialagent.media.transcriber._do_transcribe", return_value="Hola"
    ) as mock_fn:
        result = await transcribe_audio(b"audio", "audio/ogg", "my-bucket")

    assert result == "Hola"
    mock_fn.assert_called_once_with(
        b"audio", "audio/ogg", "my-bucket", "us-east-1", 2, 90, "", ()
    )


@pytest.mark.asyncio
async def test_transcribe_audio_custom_params():
    """transcribe_audio passes custom poll_interval and max_wait through."""
    with patch(
        "socialagent.media.transcriber._do_transcribe", return_value="ok"
    ) as mock_fn:
        await transcribe_audio(
            b"x",
            "audio/wav",
            "bucket",
            region="eu-west-1",
            poll_interval=5,
            max_wait=30,
        )

    mock_fn.assert_called_once_with(
        b"x", "audio/wav", "bucket", "eu-west-1", 5, 30, "", ()
    )


@pytest.mark.asyncio
async def test_transcribe_audio_with_key_prefix():
    """transcribe_audio forwards key_prefix to _do_transcribe for stack-scoped IAM."""
    with patch(
        "socialagent.media.transcriber._do_transcribe", return_value="hi"
    ) as mock_fn:
        await transcribe_audio(
            b"x", "audio/ogg", "bucket", key_prefix="stacks/scheduler-dev"
        )
    mock_fn.assert_called_once_with(
        b"x", "audio/ogg", "bucket", "us-east-1", 2, 90, "stacks/scheduler-dev", ()
    )


# ── language options ───────────────────────────────────────────────────────────


def _run(language_options, caplog=None):
    s3, transcribe, transcript_json = _make_clients(["COMPLETED"], transcript="ve")
    with (
        patch(
            "socialagent.media.transcriber.boto3.client", side_effect=[s3, transcribe]
        ),
        patch("socialagent.media.transcriber.urllib.request.urlopen") as mock_open,
    ):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=transcript_json)
        _do_transcribe(
            b"audio",
            "audio/ogg",
            "b",
            "us-east-1",
            poll_interval=0,
            timeout=10,
            language_options=language_options,
        )
    return transcribe.start_transcription_job.call_args.kwargs


def test_no_languages_identifies_among_all():
    """The default: Transcribe guesses the language with no hint (the behaviour
    that read a one-word Spanish note as English "Go")."""
    kw = _run(())
    assert kw["IdentifyLanguage"] is True
    assert "LanguageOptions" not in kw and "LanguageCode" not in kw


def test_two_or_more_languages_constrain_identification():
    kw = _run(("es-MX", "en-US"))
    assert kw["IdentifyLanguage"] is True
    assert kw["LanguageOptions"] == ["es-MX", "en-US"]


def test_one_language_is_fixed_not_identified():
    """Transcribe requires two LanguageOptions; a single language is a LanguageCode."""
    kw = _run(("es-MX",))
    assert kw["LanguageCode"] == "es-MX"
    assert kw["IdentifyLanguage"] is False
    assert "LanguageOptions" not in kw


def test_transcript_is_logged_by_length_and_language_not_words(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="socialagent.media.transcriber"):
        _run(("es-MX", "en-US"))
    line = next(
        r.message for r in caplog.records if "transcribed voice note" in r.message
    )
    assert "chars=2" in line
    assert "ve" not in line.split("chars")[0]  # the words never reach the log


def test_config_reads_transcribe_languages_from_env(monkeypatch):
    from socialagent.config import SocialAgentConfig

    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x"
    )
    monkeypatch.setenv("MEMORY_ID", "mem-1")
    monkeypatch.setenv("TRANSCRIBE_LANGUAGES", "es-MX, en-US")
    assert SocialAgentConfig.from_env().transcribe_languages == ("es-MX", "en-US")
    monkeypatch.setenv("TRANSCRIBE_LANGUAGES", "")
    assert SocialAgentConfig.from_env().transcribe_languages == ()
