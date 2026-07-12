"""FHIR attachment SSRF + bearer-token-leak guard (HIGH-4).

`resolve_attachment_url` is the security boundary: a tampered
DocumentReference.attachment.url must not make OwnChart fetch an
internal target, and even a permitted cross-host URL must never
receive the EHR bearer token.
"""

from __future__ import annotations

import pytest

from ownchart.ingest import fhir_attachments
from ownchart.ingest.fhir_attachments import (
    AttachmentUrlError,
    resolve_attachment_url,
)

BASE = "https://fhir.example.org/api/FHIR/R4"


# ---------------------------------------------------------------------------
# SSRF rejection (numeric hosts — no DNS needed)


def test_private_ip_absolute_url_rejected():
    with pytest.raises(AttachmentUrlError):
        resolve_attachment_url(BASE, "https://10.15.15.99/Binary/1")


def test_loopback_ip_absolute_url_rejected():
    with pytest.raises(AttachmentUrlError):
        resolve_attachment_url(BASE, "https://127.0.0.1/Binary/1")


def test_metadata_service_ip_rejected():
    with pytest.raises(AttachmentUrlError):
        resolve_attachment_url(BASE, "https://169.254.169.254/latest/meta-data/")


def test_cross_origin_http_rejected():
    # Plain-http is only tolerated for the same origin as the FHIR base.
    with pytest.raises(AttachmentUrlError):
        resolve_attachment_url(BASE, "http://cdn.example.net/x")


def test_non_http_scheme_rejected():
    with pytest.raises(AttachmentUrlError):
        resolve_attachment_url(BASE, "file:///etc/passwd")


# ---------------------------------------------------------------------------
# Token gating — cross-host must NOT be same-host (no token), relative /
# same-host may keep the token. `_assert_public_host` is stubbed so these
# stay offline.


def test_relative_reference_is_same_host(monkeypatch):
    monkeypatch.setattr(fhir_attachments, "_assert_public_host", lambda h: None)
    out = resolve_attachment_url(BASE, "Binary/abc123")
    assert out.same_host_as_base is True
    assert out.url == "https://fhir.example.org/api/FHIR/R4/Binary/abc123"


def test_same_host_absolute_keeps_token(monkeypatch):
    monkeypatch.setattr(fhir_attachments, "_assert_public_host", lambda h: None)
    out = resolve_attachment_url(BASE, "https://fhir.example.org/Binary/xyz")
    assert out.same_host_as_base is True


def test_cross_host_absolute_does_not_get_token(monkeypatch):
    monkeypatch.setattr(fhir_attachments, "_assert_public_host", lambda h: None)
    out = resolve_attachment_url(BASE, "https://attacker.example.net/Binary/xyz")
    assert out.same_host_as_base is False
    # URL is unchanged (we still don't fetch it with the token attached).
    assert out.url == "https://attacker.example.net/Binary/xyz"


def test_too_large_helper_uses_content_length_and_body():
    class _R:
        def __init__(self, headers, content):
            self.headers = headers
            self.content = content

    cap = fhir_attachments.MAX_ATTACHMENT_BYTES
    assert fhir_attachments._too_large(_R({"content-length": str(cap + 1)}, b"")) is True
    assert fhir_attachments._too_large(_R({}, b"x" * (cap + 1))) is True
    assert fhir_attachments._too_large(_R({}, b"small")) is False
