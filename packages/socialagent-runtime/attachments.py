# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Give the agent the original image or document a user sent.

The webhook, when ``enable_inbound_attachments`` is on, stages inbound media in the
stack's media bucket and lists it in the payload's ``attachments``. This module turns
that into what the agent is called with::

    from socialagent_runtime import attachments

    prompt = attachments.build_prompt(payload, accepts={"image", "document"})
    result = agent(prompt)

``build_prompt`` returns the plain text ``payload["prompt"]`` unless the payload lists
an attachment of a kind in ``accepts``; then it returns Converse content blocks, the
text first and one ``image`` or ``document`` block per accepted attachment, with the
bytes read from S3. An agent that never calls it, or an older payload with no
``attachments`` key, behaves exactly as before.

``accepts`` is the caller's declaration of what its model takes. It lives at the call
site, next to the model, rather than in a setting that can drift from it.

Runtime layer: imports only the standard library and boto3.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Converse API limits per block. The webhook's own cap is a separate, configurable
# setting; these are the hard ceilings the model call would reject.
CONVERSE_IMAGE_MAX_BYTES = 3_750_000
CONVERSE_DOCUMENT_MAX_BYTES = 4_500_000

_LIMITS = {"image": CONVERSE_IMAGE_MAX_BYTES, "document": CONVERSE_DOCUMENT_MAX_BYTES}

# A Converse document name may contain letters, digits, whitespace, hyphens,
# parentheses and square brackets, with no consecutive whitespace.
_NAME_BAD = re.compile(r"[^A-Za-z0-9 \-()\[\]]+")
_NAME_WS = re.compile(r"\s+")


# Converse rejects a document name over 200 characters, and a rejected request
# leaves the session failing on every later turn until the message is stripped
# from history, so the cap is enforced here, not discovered there.
_NAME_MAX = 200


def _document_name(filename: str | None) -> str:
    stem = Path(filename).stem if filename else ""
    name = _NAME_WS.sub(" ", _NAME_BAD.sub(" ", stem)).strip()[:_NAME_MAX].rstrip()
    return name or "document"


def _read(att: dict, s3: Any | None, region: str) -> bytes:
    path = att.get("path")
    if path:
        return Path(path).read_bytes()
    loc = att["s3"]
    client = s3 or boto3.client("s3", region_name=region)
    return client.get_object(Bucket=loc["bucket"], Key=loc["key"])["Body"].read()


def _block(att: dict, data: bytes) -> dict:
    kind, fmt = att["kind"], att["format"]
    if kind == "image":
        return {"image": {"format": fmt, "source": {"bytes": data}}}
    return {
        "document": {
            "format": fmt,
            "name": _document_name(att.get("filename")),
            "source": {"bytes": data},
        }
    }


def build_prompt(
    payload: dict, accepts: set[str], *, s3: Any | None = None
) -> str | list[dict]:
    """Return what to call the agent with: the text prompt, or content blocks.

    ``accepts`` names the attachment kinds the agent's model takes, ``"image"`` and/or
    ``"document"``. Any attachment that is malformed, of another kind, over the
    Converse size limit, or that cannot be read is skipped with a log line; if none
    remain, the plain prompt is returned.
    """
    prompt = payload.get("prompt", "")
    listed = payload.get("attachments") or []
    if not listed or not accepts:
        return prompt
    region = (payload.get("socialagent") or {}).get("region") or os.environ.get(
        "AWS_REGION", "us-east-1"
    )
    blocks: list[dict] = []
    for att in listed:
        kind = att.get("kind")
        if kind not in accepts:
            continue
        if (
            kind not in _LIMITS
            or not att.get("format")
            or not (att.get("s3") or att.get("path"))
        ):
            logger.warning(
                "Skipping malformed attachment: %s",
                {k: att.get(k) for k in ("kind", "format")},
            )
            continue
        size = att.get("size")
        if isinstance(size, int) and size > _LIMITS[kind]:
            logger.warning(
                "Skipping %s attachment: %d bytes exceeds the Converse limit of %d",
                kind,
                size,
                _LIMITS[kind],
            )
            continue
        try:
            data = _read(att, s3, region)
        except Exception:
            logger.warning(
                "Could not read %s attachment %s",
                kind,
                att.get("s3") or att.get("path"),
                exc_info=True,
            )
            continue
        if len(data) > _LIMITS[kind]:
            logger.warning(
                "Skipping %s attachment: %d bytes exceeds the Converse limit",
                kind,
                len(data),
            )
            continue
        blocks.append(_block(att, data))
    if not blocks:
        return prompt
    return [{"text": prompt}, *blocks]
