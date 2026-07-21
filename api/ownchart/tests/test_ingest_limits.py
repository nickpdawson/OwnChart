"""Ingest resource-boundary tests (P0-5, release review 2026-07-20).

Covers the streaming byte cap, Content-Length fast-reject, magic-byte
signature check, PDF page truncation, image decompression-bomb guard, and
file-count cap — plus the route-level contract that oversized / mismatched
uploads are rejected with STABLE error codes before any parse/render/LLM work,
and that the upload-audit header echo survives the new error paths.

Mostly pure-function / stub-driven so the suite stays DB-free and fast. The
route-level cases hit only pre-storage rejection paths (413/422), which never
touch Postgres.
"""

from __future__ import annotations

import io

import pymupdf
import pytest
from PIL import Image

from ownchart.ingest import images, limits, pdf
from .conftest import authed_client


# ---------------------------------------------------------------------------
# Stubs — version-independent stand-ins for UploadFile / Request.


class _FakeUpload:
    """Minimal UploadFile: async chunked read + close, backed by BytesIO."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)
        self.closed = False

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    async def close(self) -> None:
        self.closed = True


class _FakeRequest:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self._chunks = chunks
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    async def stream(self):
        for c in self._chunks:
            yield c


# ---------------------------------------------------------------------------
# sniff_kind + verify_upload_signature


def test_sniff_kind_recognises_containers():
    assert limits.sniff_kind(b"%PDF-1.7\n...") == "pdf"
    assert limits.sniff_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "png"
    assert limits.sniff_kind(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "jpeg"
    assert limits.sniff_kind(b"GIF89a" + b"\x00" * 8) == "gif"
    assert limits.sniff_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert limits.sniff_kind(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 4) == "heif"
    assert limits.sniff_kind(b'  {"a": 1}') == "json"
    assert limits.sniff_kind(b"<?xml version='1.0'?><doc/>") == "xml"


def test_verify_signature_pdf_mismatch_rejects():
    # PNG bytes declared as a PDF → 422 unsupported_media_type.
    with pytest.raises(limits.HTTPException) as ei:
        limits.verify_upload_signature(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "application/pdf", kind="pdf"
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == limits.CODE_UNSUPPORTED_MEDIA


def test_verify_signature_pdf_accepts_real_pdf():
    # Must not raise.
    limits.verify_upload_signature(b"%PDF-1.4\n%..." + b"\x00" * 16, "application/pdf", kind="pdf")


def test_verify_signature_image_declared_png_but_pdf_rejects():
    with pytest.raises(limits.HTTPException) as ei:
        limits.verify_upload_signature(b"%PDF-1.4" + b"0" * 16, "image/png", kind="image")
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == limits.CODE_UNSUPPORTED_MEDIA


def test_verify_signature_image_declared_png_but_jpeg_rejects():
    # Right family, wrong specific type: JPEG bytes declared as PNG.
    with pytest.raises(limits.HTTPException) as ei:
        limits.verify_upload_signature(b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/png", kind="image")
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == limits.CODE_UNSUPPORTED_MEDIA


def test_verify_signature_image_matching_type_ok():
    limits.verify_upload_signature(b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg", kind="image")


def test_verify_signature_unknown_bytes_deferred_to_decoder():
    # An unsniffable-but-plausible image body is allowed through — the decoder
    # is the final authority. Must not raise.
    limits.verify_upload_signature(b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c", "image/heic", kind="image")


# ---------------------------------------------------------------------------
# reject_if_declared_length_over — Content-Length fast path


def test_declared_length_over_cap_plus_slack_rejects():
    # cap 1 KB, slack is 1 MiB → a declared 3 MB body trips the fast reject.
    req = _FakeRequest([], headers={"content-length": str(3 * 1024 * 1024)})
    with pytest.raises(limits.HTTPException) as ei:
        limits.reject_if_declared_length_over(req, 1024)  # type: ignore[arg-type]
    assert ei.value.status_code == 413
    assert ei.value.detail["code"] == limits.CODE_TOO_LARGE
    assert ei.value.detail["max_bytes"] == 1024


def test_declared_length_within_slack_passes():
    # Body at exactly the cap reports CL slightly over (multipart overhead);
    # the slack keeps it from false-positive rejecting.
    req = _FakeRequest([], headers={"content-length": str(1024 + 500)})
    limits.reject_if_declared_length_over(req, 1024)  # must not raise


def test_declared_length_absent_or_garbage_passes():
    limits.reject_if_declared_length_over(_FakeRequest([]), 1024)  # type: ignore[arg-type]
    limits.reject_if_declared_length_over(
        _FakeRequest([], headers={"content-length": "not-a-number"}), 1024,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# read_upload_capped / read_body_capped — the authoritative streaming counter


@pytest.mark.asyncio
async def test_read_upload_capped_under_limit_returns_bytes():
    up = _FakeUpload(b"x" * 500)
    out = await limits.read_upload_capped(up, max_bytes=1024)  # type: ignore[arg-type]
    assert out == b"x" * 500


@pytest.mark.asyncio
async def test_read_upload_capped_over_limit_413():
    up = _FakeUpload(b"x" * 4096)
    with pytest.raises(limits.HTTPException) as ei:
        await limits.read_upload_capped(up, max_bytes=1024)  # type: ignore[arg-type]
    assert ei.value.status_code == 413
    assert ei.value.detail["code"] == limits.CODE_TOO_LARGE
    assert up.closed is True


@pytest.mark.asyncio
async def test_read_body_capped_counts_across_chunks():
    # A "Content-Length lie": no CL header, but the streamed body exceeds the
    # cap across chunks. The running counter — not the header — catches it.
    req = _FakeRequest([b"a" * 600, b"b" * 600])
    with pytest.raises(limits.HTTPException) as ei:
        await limits.read_body_capped(req, max_bytes=1024)  # type: ignore[arg-type]
    assert ei.value.status_code == 413
    assert ei.value.detail["code"] == limits.CODE_TOO_LARGE


@pytest.mark.asyncio
async def test_read_body_capped_under_limit_ok():
    req = _FakeRequest([b"a" * 300, b"b" * 300])
    out = await limits.read_body_capped(req, max_bytes=1024)  # type: ignore[arg-type]
    assert out == b"a" * 300 + b"b" * 300


# ---------------------------------------------------------------------------
# enforce_file_count


def test_enforce_file_count_over_limit(monkeypatch):
    monkeypatch.setattr(limits, "max_files_per_request", lambda: 3)
    with pytest.raises(limits.HTTPException) as ei:
        limits.enforce_file_count([object()] * 4)  # type: ignore[list-item]
    assert ei.value.status_code == 413
    assert ei.value.detail["code"] == limits.CODE_TOO_MANY_FILES
    assert ei.value.detail["max_files"] == 3


def test_enforce_file_count_at_limit_ok(monkeypatch):
    monkeypatch.setattr(limits, "max_files_per_request", lambda: 3)
    limits.enforce_file_count([object()] * 3)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# PDF page truncation — provenance stays honest


def _make_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_render_pdf_truncates_at_cap_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_renders_dir", lambda sid: tmp_path / sid)
    out = pdf.render_pdf(_make_pdf(5), "src-trunc", dpi=36, max_pages=2)
    assert out.page_count == 2           # rendered
    assert out.total_page_count == 5     # actual
    assert out.truncated is True
    assert len(out.pages) == 2
    assert out.pdf_metadata["pages_truncated"] is True
    assert out.pdf_metadata["total_page_count"] == 5


def test_render_pdf_under_cap_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_renders_dir", lambda sid: tmp_path / sid)
    out = pdf.render_pdf(_make_pdf(2), "src-ok", dpi=36, max_pages=200)
    assert out.page_count == 2
    assert out.total_page_count == 2
    assert out.truncated is False


# ---------------------------------------------------------------------------
# Image decompression-bomb guard


def _png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_analyze_rejects_pixel_bomb(tmp_path, monkeypatch):
    # Lower the cap so a tiny image trips the guard without allocating one.
    monkeypatch.setattr(images, "max_image_pixels", lambda: 100)  # 10x10
    monkeypatch.setattr(images, "_renders_dir", lambda sid: tmp_path / sid)
    with pytest.raises(limits.PixelLimitExceeded) as ei:
        images.analyze_and_thumbnail(_png_bytes(50, 50), "bomb")  # 2500 px > 100
    assert ei.value.max_pixels == 100


def test_analyze_allows_under_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "max_image_pixels", lambda: 100_000_000)
    monkeypatch.setattr(images, "_renders_dir", lambda sid: tmp_path / sid)
    meta = images.analyze_and_thumbnail(_png_bytes(40, 40), "ok")
    assert meta.width == 40 and meta.height == 40


# ---------------------------------------------------------------------------
# Route-level contract — stable codes + upload-audit echo, pre-storage.


_AUDIT_HEADERS = {"X-Client-Batch-Id": "batch-77", "X-Client-Item-Id": "item-9"}


def test_photo_oversized_returns_413_code_and_echoes_audit(app_fixture, monkeypatch):
    monkeypatch.setattr(limits, "max_attachment_bytes", lambda: 512)
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post(
        "/api/sources/photo",
        files={"file": ("x.jpg", b"\xff\xd8\xff\xe0" + b"0" * 2048, "image/jpeg")},
        headers=_AUDIT_HEADERS,
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["detail"]["code"] == limits.CODE_TOO_LARGE
    # Upload-audit echo survives the new cap path (repo doctrine).
    assert body["upload_audit"] == {"client_batch_id": "batch-77", "client_item_id": "item-9"}


def test_photo_magic_mismatch_returns_422(app_fixture):
    client = authed_client(app_fixture, role="caregiver")
    # Declared PNG, but the bytes are a PDF. >8KB to clear the min-bytes gate.
    resp = client.post(
        "/api/sources/photo",
        files={"file": ("fake.png", b"%PDF-1.4\n" + b"0" * 9000, "image/png")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == limits.CODE_UNSUPPORTED_MEDIA


def test_pdf_oversized_returns_413(app_fixture, monkeypatch):
    monkeypatch.setattr(limits, "max_attachment_bytes", lambda: 512)
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post(
        "/api/sources/pdf",
        files={"file": ("d.pdf", b"%PDF-1.4\n" + b"0" * 2048, "application/pdf")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == limits.CODE_TOO_LARGE


def test_pdf_magic_mismatch_returns_422(app_fixture):
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post(
        "/api/sources/pdf",
        files={"file": ("d.pdf", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "application/pdf")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == limits.CODE_UNSUPPORTED_MEDIA


def test_voice_oversized_returns_413(app_fixture, monkeypatch):
    monkeypatch.setattr(limits, "max_attachment_bytes", lambda: 512)
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post(
        "/api/sources/voice",
        files={"file": ("v.m4a", b"0" * 2048, "audio/m4a")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == limits.CODE_TOO_LARGE


def test_auto_export_file_oversized_returns_413(app_fixture, monkeypatch):
    monkeypatch.setattr(limits, "max_attachment_bytes", lambda: 512)
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post(
        "/api/sources/auto-export",
        files={"file": ("ae.json", b"0" * 2048, "application/json")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == limits.CODE_TOO_LARGE


def test_ccda_too_many_files_returns_413(app_fixture, monkeypatch):
    monkeypatch.setattr(limits, "max_files_per_request", lambda: 2)
    client = authed_client(app_fixture, role="caregiver")
    files = [("files", (f"c{i}.xml", b"<?xml ?><ClinicalDocument/>", "application/xml")) for i in range(3)]
    resp = client.post("/api/sources/ccda", files=files)
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == limits.CODE_TOO_MANY_FILES
