# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Stage an inbound image or document in S3 so the agent can receive the original.

Without this, the only thing an agent sees of a photo or a PDF is whatever text
the ``on_media`` hook returns. The webhook can instead write the file to the
stack's media bucket and list it in the payload's ``attachments``; a project then
opts in, agent-side, with ``socialagent_runtime.attachments.build_prompt``. The
text prompt is still built and sent, so an agent that ignores ``attachments``
behaves exactly as before.

Only the Lambda layer may download WhatsApp media (``socialmessaging`` is confined
to the provider), and the object key is derived from the message id and the MIME
type, never from anything the sender controls.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import boto3

from socialagent.providers.eum_social import MediaTooLarge

from socialagent.types import InboundMessage, MessageType

if TYPE_CHECKING:
    from socialagent.config import SocialAgentConfig
    from socialagent.providers.base import MessageProvider

logger = logging.getLogger(__name__)

# Objects land under this prefix (beneath the stack's key prefix on a shared
# bucket). The Terraform lifecycle rule and the runtime role's read grant name the
# same prefix, so the three must change together.
ATTACHMENTS_PREFIX = "media/inbound/"

# MIME → (kind, Converse format). Only what the Converse API accepts as an image
# or document block; anything else is left to the text prompt.
_FORMATS: dict[str, tuple[str, str]] = {
    "image/jpeg": ("image", "jpeg"),
    "image/jpg": ("image", "jpeg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "image/webp": ("image", "webp"),
    "application/pdf": ("document", "pdf"),
    "text/csv": ("document", "csv"),
    "application/msword": ("document", "doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "document",
        "docx",
    ),
    "application/vnd.ms-excel": ("document", "xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        "document",
        "xlsx",
    ),
    "text/html": ("document", "html"),
    "text/plain": ("document", "txt"),
    "text/markdown": ("document", "md"),
}

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _kind_and_format(mime_type: str) -> tuple[str, str] | None:
    return _FORMATS.get(mime_type.split(";", 1)[0].strip().lower())


def attachment_key(prefix: str, message_id: str, mime_type: str) -> str | None:
    """Return the S3 key for a message's media, or ``None`` if the type is unsupported.

    The key is ``<prefix>/media/inbound/<message id>.<format>``; the id is reduced to
    a safe character set so no sender-controlled byte reaches the key.
    """
    kf = _kind_and_format(mime_type)
    if kf is None:
        return None
    safe_id = _SAFE_ID.sub("", message_id).strip("._-") or "message"
    base = f"{ATTACHMENTS_PREFIX}{safe_id}.{kf[1]}"
    prefix = prefix.strip("/")
    return f"{prefix}/{base}" if prefix else base


async def stage_attachment(
    message: InboundMessage,
    config: SocialAgentConfig,
    provider: MessageProvider,
    *,
    s3: Any | None = None,
) -> dict | None:
    """Download the message's media, write it to S3, and describe it for the payload.

    Returns ``None``, logging why, whenever the file cannot or should not be staged:
    not an image or document, unsupported type, no media bucket, over the size cap,
    or a download or upload failure. The caller then sends the text prompt alone.
    """
    if (
        message.type not in (MessageType.IMAGE, MessageType.DOCUMENT)
        or not message.media
    ):
        return None
    media = message.media
    kf = _kind_and_format(media.mime_type)
    if kf is None:
        logger.info(
            "Attachment %s has unsupported type %s", message.id, media.mime_type
        )
        return None
    bucket = config.media_bucket
    if not bucket:
        logger.warning(
            "Attachment %s not staged: no media bucket configured", message.id
        )
        return None
    key = attachment_key(config.media_prefix, message.id, media.mime_type)
    try:
        # The provider knows the size from the S3 listing before it reads the
        # object, so the cap is enforced there and an oversize file is never
        # read into the Lambda.
        data = await provider.download_media(
            media.id, max_bytes=config.attachments_max_bytes
        )
    except MediaTooLarge as exc:
        logger.warning(
            "Attachment %s is %d bytes, over the %d-byte cap; not staged",
            message.id,
            exc.size,
            exc.max_bytes,
        )
        return None
    except Exception:
        logger.warning("Attachment %s download failed", message.id, exc_info=True)
        return None
    if len(data) > config.attachments_max_bytes:
        # A provider that ignores max_bytes (a test double, another backend)
        # still gets the cap applied here.
        logger.warning(
            "Attachment %s is %d bytes, over the %d-byte cap; not staged",
            message.id,
            len(data),
            config.attachments_max_bytes,
        )
        return None
    client = s3 or boto3.client("s3", region_name=config.region)
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=media.mime_type.split(";", 1)[0].strip(),
        )
    except Exception:
        logger.warning(
            "Attachment %s upload to s3://%s/%s failed",
            message.id,
            bucket,
            key,
            exc_info=True,
        )
        return None
    kind, fmt = kf
    return {
        "kind": kind,
        "format": fmt,
        "s3": {"bucket": bucket, "key": key},
        "filename": media.filename,
        "caption": media.caption,
        "size": len(data),
    }
