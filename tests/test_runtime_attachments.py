# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``socialagent_runtime.attachments.build_prompt``: turn the payload into what the
agent is called with. No attachments, or none the caller accepts, gives back the
plain text prompt, byte for byte; otherwise Converse content blocks with the
original bytes. The old payload shape (no ``attachments`` key) must keep working,
so the payload version does not move.
"""

from unittest.mock import MagicMock

import pytest

from socialagent_runtime import _validate_payload
from socialagent_runtime.attachments import (
    CONVERSE_IMAGE_MAX_BYTES,
    build_prompt,
)


def _payload(prompt="[User sent an image: a receipt]", attachments=None) -> dict:
    p = {
        "prompt": prompt,
        "phone": "+525555550100",
        "socialagent": {
            "version": 1,
            "memory_id": "m",
            "session_id": "s",
            "actor_id": "a",
            "message_id": "wamid.1",
            "message_type": "image",
            "region": "us-east-1",
        },
    }
    if attachments is not None:
        p["attachments"] = attachments
    return p


def _image(**kw) -> dict:
    return {
        "kind": "image",
        "format": "jpeg",
        "s3": {"bucket": "b", "key": "p/media/inbound/wamid.1.jpeg"},
        "filename": None,
        "caption": "a receipt",
        "size": 3,
        **kw,
    }


def _doc(**kw) -> dict:
    return {
        "kind": "document",
        "format": "pdf",
        "s3": {"bucket": "b", "key": "p/media/inbound/wamid.2.pdf"},
        "filename": "invoice.pdf",
        "caption": None,
        "size": 8,
        **kw,
    }


def _s3(body: bytes) -> MagicMock:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
    return s3


# --- compatibility ------------------------------------------------------------


def test_payload_without_attachments_still_validates():
    _validate_payload(_payload())


def test_payload_with_attachments_still_validates():
    _validate_payload(_payload(attachments=[_image()]))


def test_no_attachments_returns_the_prompt_string():
    assert (
        build_prompt(_payload(), accepts={"image"}) == "[User sent an image: a receipt]"
    )


def test_empty_accepts_returns_the_prompt_string():
    assert build_prompt(_payload(attachments=[_image()]), accepts=set()) == (
        "[User sent an image: a receipt]"
    )


def test_unaccepted_kind_returns_the_prompt_string():
    out = build_prompt(_payload(attachments=[_doc()]), accepts={"image"}, s3=_s3(b"x"))
    assert out == "[User sent an image: a receipt]"


# --- content blocks -----------------------------------------------------------


def test_image_becomes_text_plus_image_block():
    out = build_prompt(
        _payload(attachments=[_image()]), accepts={"image"}, s3=_s3(b"jpg")
    )
    assert out == [
        {"text": "[User sent an image: a receipt]"},
        {"image": {"format": "jpeg", "source": {"bytes": b"jpg"}}},
    ]


def test_document_block_carries_name_and_format():
    out = build_prompt(
        _payload(attachments=[_doc()]), accepts={"document"}, s3=_s3(b"%PDF")
    )
    assert out[1] == {
        "document": {"format": "pdf", "name": "invoice", "source": {"bytes": b"%PDF"}}
    }


def test_document_without_filename_gets_a_safe_name():
    out = build_prompt(
        _payload(attachments=[_doc(filename=None)]),
        accepts={"document"},
        s3=_s3(b"%PDF"),
    )
    assert out[1]["document"]["name"] == "document"


def test_document_name_is_sanitised_for_converse():
    # Converse allows letters, digits, whitespace, hyphen, parentheses and square
    # brackets, and no consecutive whitespace. Everything else must go.
    out = build_prompt(
        _payload(attachments=[_doc(filename="Q3  report_v2 (final).pdf")]),
        accepts={"document"},
        s3=_s3(b"%PDF"),
    )
    assert out[1]["document"]["name"] == "Q3 report v2 (final)"


def test_reads_the_object_the_payload_names():
    s3 = _s3(b"jpg")
    build_prompt(_payload(attachments=[_image()]), accepts={"image"}, s3=s3)
    s3.get_object.assert_called_once_with(
        Bucket="b", Key="p/media/inbound/wamid.1.jpeg"
    )


def test_oversize_declared_attachment_is_skipped_without_download():
    s3 = _s3(b"x")
    att = _image(size=CONVERSE_IMAGE_MAX_BYTES + 1)
    out = build_prompt(_payload(attachments=[att]), accepts={"image"}, s3=s3)
    assert out == "[User sent an image: a receipt]"
    s3.get_object.assert_not_called()


def test_download_failure_falls_back_to_the_prompt_string():
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("denied")
    out = build_prompt(_payload(attachments=[_image()]), accepts={"image"}, s3=s3)
    assert out == "[User sent an image: a receipt]"


def test_mixed_attachments_keep_only_accepted_kinds():
    out = build_prompt(
        _payload(attachments=[_image(), _doc()]), accepts={"document"}, s3=_s3(b"%PDF")
    )
    assert [k for b in out for k in b] == ["text", "document"]


# --- dev mode -----------------------------------------------------------------


def test_local_path_attachment_is_read_from_disk(tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"png!")
    att = _image(format="png", s3=None, path=str(f))
    out = build_prompt(_payload(attachments=[att]), accepts={"image"})
    assert out[1] == {"image": {"format": "png", "source": {"bytes": b"png!"}}}


@pytest.mark.parametrize(
    "bad", [{"kind": "image"}, {"kind": "image", "format": "jpeg"}]
)
def test_malformed_attachment_is_skipped(bad):
    out = build_prompt(_payload(attachments=[bad]), accepts={"image"}, s3=_s3(b"x"))
    assert out == "[User sent an image: a receipt]"


def test_document_name_is_capped_at_converse_limit():
    long_name = "a" * 300 + ".pdf"
    out = build_prompt(
        _payload(attachments=[_doc(filename=long_name)]),
        accepts={"document"},
        s3=_s3(b"%PDF"),
    )
    assert len(out[1]["document"]["name"]) == 200
