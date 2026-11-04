# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for hardening: version validation, SNS signature, structured logging."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from socialagent.constants import SOCIALAGENT_VERSION
from socialagent.providers.eum_social import EUMSocialProvider, _verify_sns_signature

# The real _validate_payload; conftest stubs bedrock_agentcore and puts
# packages/socialagent-runtime on the path, so import it rather than mirroring it (a mirror
# silently drifts from the code it claims to cover).
_AGENT_DIR = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from socialagent_runtime import _validate_payload  # noqa: E402


# ── socialagent-runtime version validation ─────────────────────────────────────────────


def test_validate_payload_raises_on_missing_required_key():
    """A malformed socialagent block must fail with a clear ValueError, not a KeyError.

    memory_id/session_id/actor_id are indexed directly by create_session_manager;
    an absent one used to crash the invocation with a bare KeyError before the
    agent ran.
    """
    with pytest.raises(ValueError, match="missing required field"):
        _validate_payload(
            {"socialagent": {"version": SOCIALAGENT_VERSION, "memory_id": "m"}}
        )


def test_validate_payload_raises_on_missing_socialagent_block():
    """_validate_payload raises ValueError when 'socialagent' block is absent."""
    with pytest.raises(ValueError, match="missing 'socialagent' block"):
        _validate_payload({"prompt": "hi"})


def test_validate_payload_raises_on_version_mismatch():
    """_validate_payload raises ValueError on version mismatch."""
    with pytest.raises(ValueError, match="version mismatch"):
        _validate_payload(
            {
                "socialagent": {
                    "version": 999,
                    "memory_id": "m",
                    "session_id": "s",
                    "actor_id": "a",
                }
            }
        )


def test_validate_payload_passes_on_correct_version():
    """_validate_payload returns the socialagent block when version matches."""
    socialagent_block = {
        "version": SOCIALAGENT_VERSION,
        "memory_id": "m",
        "session_id": "s",
        "actor_id": "a",
    }
    result = _validate_payload({"socialagent": socialagent_block})
    assert result == socialagent_block


# ── SNS signature verification ─────────────────────────────────────────────────


def test_verify_sns_signature_rejects_when_no_cert_url():
    """Fails closed when SigningCertURL is absent — an SNS envelope with
    no signature fields cannot be verified."""
    assert _verify_sns_signature({"Type": "Notification", "Message": "hi"}) is False


def test_verify_sns_signature_rejects_when_no_signature():
    """Fails closed when the Signature field is absent."""
    assert (
        _verify_sns_signature(
            {"SigningCertURL": "https://sns.us-east-1.amazonaws.com/cert.pem"}
        )
        is False
    )


def test_verify_sns_signature_rejects_suspicious_cert_url():
    """Returns False when cert URL does not look like an SNS URL."""
    sns = {
        "SigningCertURL": "https://evil.com/cert.pem",
        "Signature": "abc",
        "Type": "Notification",
    }
    assert _verify_sns_signature(sns) is False


def test_verify_sns_signature_rejects_on_crypto_error():
    """Fails closed when cert fetch or crypto verification fails — a
    transient fetch error is not traded for authenticity (SNS retries delivery)."""
    sns = {
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/cert.pem",
        "Signature": "invalidsig==",
        "Type": "Notification",
        "Message": "hi",
        "MessageId": "1",
        "Timestamp": "2024-01-01T00:00:00Z",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
    }
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        side_effect=OSError("network"),
    ):
        assert _verify_sns_signature(sns) is False


def _make_signed_sns_envelope(
    sns_partial: dict, signed_keys: tuple[str, ...]
) -> tuple[dict, bytes]:
    """Build a real SNS envelope signed with a freshly generated RSA cert.

    Returns (envelope, cert_pem). The canonical string is built from
    ``signed_keys`` in order, including a key only when its value is non-None —
    exactly SNS's rule — so tests can assert the verifier matches it.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes as _h
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding as _pad
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .sign(key, _h.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    string_to_sign = "".join(
        f"{k}\n{sns_partial[k]}\n"
        for k in signed_keys
        if sns_partial.get(k) is not None
    ).encode()
    sig = key.sign(string_to_sign, _pad.PKCS1v15(), _h.SHA1())

    envelope = dict(sns_partial)
    envelope["Signature"] = base64.b64encode(sig).decode()
    envelope["SigningCertUrl"] = "https://sns.us-east-1.amazonaws.com/cert.pem"
    return envelope, cert_pem


def test_verify_sns_signature_accepts_lambda_envelope_with_null_subject():
    """Regression: Lambda-delivered envelopes carry "Subject": null when no subject
    was set at publish time. SNS excludes null-valued keys from the signed string,
    so the verifier must skip them (test presence AND non-null) — testing key
    presence alone appended "Subject\\nNone\\n" and broke every real message."""
    sns_partial = {
        "Type": "Notification",
        "MessageId": "abc-123",
        "TopicArn": "arn:aws:sns:us-east-1:123:dev-socialagent-whatsapp",
        "Message": json.dumps({"whatsAppWebhookEntry": "{}"}),
        "Timestamp": "2026-07-14T05:58:00.000Z",
        "Subject": None,  # present-but-null, as SNS→Lambda delivers it
        "SignatureVersion": "1",
    }
    signed_keys = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
    envelope, cert_pem = _make_signed_sns_envelope(sns_partial, signed_keys)

    class _FakeResp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return cert_pem

    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        return_value=_FakeResp(),
    ):
        assert _verify_sns_signature(envelope) is True


def test_verify_sns_signature_accepts_lambda_delivery_cert_url_casing():
    """SNS→Lambda deliveries spell the key "SigningCertUrl" (camelCase Url), not
    the HTTP-style "SigningCertURL". The verifier must read both — reading only
    the all-caps form silently drops every real Lambda-delivered notification.
    This reaches the crypto path (fails there, not on missing-fields)."""
    sns = {
        "SigningCertUrl": "https://sns.us-east-1.amazonaws.com/cert.pem",
        "Signature": "invalidsig==",
        "Type": "Notification",
        "Message": "hi",
        "MessageId": "1",
        "Timestamp": "2024-01-01T00:00:00Z",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
    }
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        side_effect=OSError("network"),
    ) as m:
        assert _verify_sns_signature(sns) is False
        # Must have gotten past the missing-fields guard to attempt cert fetch.
        assert m.called


# ── SNS signing-cert URL validation, fetch timeout and cache ─────────────────────

_GOOD_CERT_URL = (
    "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-0123abcd.pem"
)


class _CertResponse:
    """What urlopen returns: a context manager whose read() is the cert PEM."""

    def __init__(self, cert_pem: bytes) -> None:
        self._cert_pem = cert_pem

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._cert_pem


def _signed_notification(cert_url: str = _GOOD_CERT_URL) -> tuple[dict, bytes]:
    """A validly signed Notification envelope pointing at ``cert_url``."""
    sns_partial = {
        "Type": "Notification",
        "MessageId": "abc-123",
        "TopicArn": "arn:aws:sns:us-east-1:111122223333:dev-socialagent-whatsapp",
        "Message": json.dumps({"whatsAppWebhookEntry": "{}"}),
        "Timestamp": "2026-07-14T05:58:00.000Z",
        "SignatureVersion": "1",
    }
    signed_keys = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
    envelope, cert_pem = _make_signed_sns_envelope(sns_partial, signed_keys)
    envelope["SigningCertUrl"] = cert_url  # not part of the signed string
    return envelope, cert_pem


@pytest.mark.parametrize(
    "cert_url",
    [
        # Both passed the old startswith("https://sns.") + ".amazonaws.com/" test,
        # so the sender chose the host serving the cert its signature is checked
        # against: any forged message verified.
        "https://sns.example.org/.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com.example.org/x.amazonaws.com/c.pem",
        "http://sns.us-east-1.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com/cert.txt",
        "https://sns.us-east-1.amazonaws.com:8443/cert.pem",
        "https://evil.example@sns.us-east-1.amazonaws.com/cert.pem",
        "https://evil.example\\@sns.us-east-1.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com./cert.pem",
        "https://sns.a.b.amazonaws.com/cert.pem",
        "https://s3.us-east-1.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com:notaport/cert.pem",
        # S3 virtual-hosted endpoints of a bucket named "sns": the middle label
        # must be a region, not any DNS label.
        "https://sns.s3.amazonaws.com/cert.pem",
        "https://sns.s3-us-west-2.amazonaws.com/cert.pem",
        "https://sns.s3-accelerate.amazonaws.com/cert.pem",
        "https://sns.s3-external-1.amazonaws.com/cert.pem",
        "https://sns.s3-fips-us-gov-west-1.amazonaws.com/cert.pem",
    ],
)
def test_verify_sns_signature_rejects_a_non_sns_cert_host(cert_url):
    """The cert URL must be an SNS endpoint; anything else is never fetched."""
    envelope, cert_pem = _signed_notification(cert_url)
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        return_value=_CertResponse(cert_pem),
    ) as m:
        assert _verify_sns_signature(envelope) is False
    m.assert_not_called()


@pytest.mark.parametrize(
    "cert_url",
    [
        _GOOD_CERT_URL,
        "https://sns.cn-north-1.amazonaws.com.cn/SimpleNotificationService-a.pem",
        "https://sns.us-gov-west-1.amazonaws.com/SimpleNotificationService-a.pem",
        "HTTPS://SNS.US-EAST-1.AMAZONAWS.COM/SimpleNotificationService-a.pem",
        "https://sns.ap-southeast-5.amazonaws.com/SimpleNotificationService-a.pem",
        "https://sns.mx-central-1.amazonaws.com/SimpleNotificationService-a.pem",
        "https://sns.cn-northwest-1.amazonaws.com.cn/SimpleNotificationService-a.pem",
    ],
)
def test_sns_cert_fetch_url_accepts_sns_endpoints(cert_url):
    from socialagent.providers.eum_social import _sns_cert_fetch_url

    fetch_url = _sns_cert_fetch_url(cert_url)
    assert fetch_url is not None
    assert fetch_url.startswith("https://sns.")


def test_sns_cert_fetch_url_rebuilds_the_url_from_the_validated_parts():
    """What is fetched is what was checked: no query, fragment or userinfo."""
    from socialagent.providers.eum_social import _sns_cert_fetch_url

    assert _sns_cert_fetch_url(f"{_GOOD_CERT_URL}?x=1#frag") == _GOOD_CERT_URL


def test_verify_sns_signature_fetches_the_cert_with_a_timeout():
    """A cert fetch with no timeout can hang the webhook Lambda until it dies."""
    envelope, cert_pem = _signed_notification()
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        return_value=_CertResponse(cert_pem),
    ) as m:
        assert _verify_sns_signature(envelope) is True
    m.assert_called_once_with(_GOOD_CERT_URL, timeout=5)


def test_verify_sns_signature_caches_the_cert_by_url():
    """A warm Lambda fetches each signing cert once, not once per message."""
    envelope, cert_pem = _signed_notification()
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        return_value=_CertResponse(cert_pem),
    ) as m:
        assert _verify_sns_signature(envelope) is True
        assert _verify_sns_signature(envelope) is True
    assert m.call_count == 1


def test_verify_sns_signature_does_not_cache_a_failed_fetch():
    """A transient fetch error fails that message closed, then is retried."""
    envelope, cert_pem = _signed_notification()
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        side_effect=[TimeoutError("timed out"), _CertResponse(cert_pem)],
    ) as m:
        assert _verify_sns_signature(envelope) is False
        assert _verify_sns_signature(envelope) is True
    assert m.call_count == 2


def test_verify_sns_signature_cached_cert_still_checks_every_signature():
    """The cache holds the key, not the verdict: a bad signature still fails."""
    envelope, cert_pem = _signed_notification()
    with patch(
        "socialagent.providers.eum_social._SNS_CERT_OPENER.open",
        return_value=_CertResponse(cert_pem),
    ):
        assert _verify_sns_signature(envelope) is True
        tampered = {**envelope, "Message": json.dumps({"forged": True})}
        assert _verify_sns_signature(tampered) is False


def test_sns_signing_key_cache_is_bounded():
    from socialagent.providers.eum_social import _sns_signing_key

    assert _sns_signing_key.cache_info().maxsize == 8


# ── SNS signature verification in parse_webhook ────────────────────────────────


@pytest.mark.asyncio
async def test_parse_webhook_drops_event_on_signature_failure():
    """parse_webhook returns None when SNS signature verification fails."""
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    sns_body = {
        "SigningCertURL": "https://evil.com/cert.pem",
        "Signature": "bad",
        "Type": "Notification",
        "Message": json.dumps({}),
    }
    event = {"Records": [{"Sns": sns_body}]}
    result = await p.parse_webhook(event)
    assert result is None


@pytest.mark.asyncio
async def test_parse_webhook_passes_when_no_sns_signature():
    """parse_webhook processes events without SNS signature fields normally."""
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    result = await p.parse_webhook({"entry": []})
    assert result is None  # no messages, but not dropped


def test_sns_cert_fetch_refuses_redirects():
    """A redirect could move the fetch off the validated sns.<region> host."""
    import urllib.request

    from socialagent.providers.eum_social import _NoRedirect

    req = urllib.request.Request(_GOOD_CERT_URL)
    assert (
        _NoRedirect().redirect_request(
            req, None, 302, "Found", {}, "https://evil.example/c.pem"
        )
        is None
    )
    # The opener the fetch uses carries that handler instead of the default one.
    from socialagent.providers.eum_social import _SNS_CERT_OPENER

    assert any(isinstance(h, _NoRedirect) for h in _SNS_CERT_OPENER.handlers)
    assert not any(
        type(h) is urllib.request.HTTPRedirectHandler for h in _SNS_CERT_OPENER.handlers
    )
