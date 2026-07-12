"""NUL-byte sanitizer tests (HIGH-1 — whole-sync rollback class).

Postgres text/jsonb cannot store `\\x00`; asyncpg raises
UntranslatableCharacterError and rolls the transaction back. These
guard the sanitizer + the two ingest lanes (FHIR attachment plaintext
/ metadata, PDF text layer) that decode untrusted bytes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pymupdf

from ownchart.ingest.text_sanitize import strip_nul
from ownchart.ingest import fhir_attachments
from ownchart.ingest import pdf as pdf_ingest


# ---------------------------------------------------------------------------
# strip_nul


def test_strip_nul_removes_from_string():
    assert strip_nul("ASCII\x00\x00text") == "ASCIItext"


def test_strip_nul_passthrough_scalars():
    assert strip_nul(None) is None
    assert strip_nul(7) == 7
    assert strip_nul(3.5) == 3.5
    assert strip_nul(True) is True


def test_strip_nul_recurses_dict_and_list():
    out = strip_nul({"a\x00": ["x\x00y", {"b": "c\x00"}], "n": 1})
    assert out == {"a": ["xy", {"b": "c"}], "n": 1}


def test_strip_nul_bytes():
    assert strip_nul(b"a\x00b") == b"ab"


# ---------------------------------------------------------------------------
# FHIR attachment plaintext + metadata


def test_extract_plaintext_strips_nul():
    raw = b"Clinical note body\x00 with an embedded nul byte."
    out = fhir_attachments._extract_plaintext(raw, "text/plain")
    assert out is not None
    assert "\x00" not in out
    assert "embedded nul byte" in out


def test_summary_for_metadata_strips_nul_in_title():
    ref = fhir_attachments.AttachmentRef(
        source_resource_type="DocumentReference",
        source_resource_id="1",
        content_index=0,
        content_type="text/plain",
        url=None,
        inline_b64=None,
        title="Op\x00 Note",
        creation="2026-05-01",
    )
    att = fhir_attachments.FetchedAttachment(
        ref=ref, bytes_=b"x", mime="text/plain", plaintext="ok",
    )
    meta = fhir_attachments.summary_for_metadata(att, None)
    assert meta["title"] == "Op Note"
    assert "\x00" not in meta["title"]


# ---------------------------------------------------------------------------
# PDF text layer — real render, NUL in the extractable text


def test_render_pdf_strips_nul_from_text_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pdf_ingest, "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    doc = pymupdf.open()
    page = doc.new_page()
    # pymupdf preserves an embedded NUL in the text layer on readback.
    page.insert_text((72, 72), "Anesthesia\x00 record present here today")
    blob = doc.tobytes()

    out = pdf_ingest.render_pdf(blob, "test-source-nul")
    joined = " ".join(p.text_layer or "" for p in out.pages)
    assert "\x00" not in joined
    assert "Anesthesia" in joined and "record present" in joined
