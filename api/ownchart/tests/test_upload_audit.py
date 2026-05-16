"""Upload-audit correlation tests.

Pure-function: the audit module is a stateless shim — no DB, no LLM,
just header → dict and dict → raw_metadata helpers.

Doctrine: every upload request can carry X-Client-Batch-Id and
X-Client-Item-Id headers; the server stamps them on raw_metadata so
admin queries can group a batch, echoes them in error JSON so iOS
can map a 4xx/5xx back to its local (item_id → file) map, and logs
both fields so a single `grep` against the batch id surfaces every
server-side log line for that batch.
"""

from __future__ import annotations

from types import SimpleNamespace

from ownchart.core.upload_audit import (
    upload_audit_dep,
    upload_audit_from_request,
    stamp_raw_metadata,
)


# ---------------------------------------------------------------------------
# upload_audit_from_request — used by the catch-all error handler.


def _fake_request(headers: dict[str, str]):
    # Headers in real FastAPI are case-insensitive; the lookup function
    # uses lowercase keys to read them. Mirror that here.
    norm = {k.lower(): v for k, v in headers.items()}
    return SimpleNamespace(headers=norm)


def test_returns_none_when_no_headers_present():
    """Don't pollute generic API errors with empty upload_audit blocks."""
    assert upload_audit_from_request(_fake_request({})) is None


def test_returns_both_ids_when_both_set():
    out = upload_audit_from_request(_fake_request({
        "X-Client-Batch-Id": "batch-2026-05-16-abc",
        "X-Client-Item-Id": "item-007",
    }))
    assert out == {
        "client_batch_id": "batch-2026-05-16-abc",
        "client_item_id": "item-007",
    }


def test_returns_partial_when_only_batch_set():
    """iOS may set batch but not item if it's a single-file upload from
    inside a logically named batch (e.g. a forced re-extract retry)."""
    out = upload_audit_from_request(_fake_request({
        "x-client-batch-id": "retry-batch",
    }))
    assert out == {"client_batch_id": "retry-batch"}


def test_caps_oversized_header_values():
    """Defense in depth: a misbehaving client can't store a 1MB batch
    id on every SourceDocument. Truncate at 128 chars."""
    long_id = "x" * 5000
    out = upload_audit_from_request(_fake_request({
        "X-Client-Batch-Id": long_id,
    }))
    assert out is not None
    assert len(out["client_batch_id"]) == 128


# ---------------------------------------------------------------------------
# upload_audit_dep — the FastAPI dependency.


def test_dep_returns_none_when_no_headers():
    assert upload_audit_dep(None, None) is None


def test_dep_returns_dict_when_headers_passed():
    out = upload_audit_dep("batch-1", "item-42")
    assert out == {"client_batch_id": "batch-1", "client_item_id": "item-42"}


# ---------------------------------------------------------------------------
# stamp_raw_metadata — merges audit dict into SourceDocument metadata.


def test_stamp_noop_when_audit_none():
    """Server-originated rows (FHIR sync, CCDA pull) have no client-side
    audit info — raw_metadata stays clean."""
    rm = {"format": "PNG", "width": 1170}
    out = stamp_raw_metadata(rm, None)
    assert out == rm
    assert "upload_audit" not in out


def test_stamp_adds_upload_audit_key():
    rm = {"format": "PNG", "width": 1170}
    audit = {"client_batch_id": "b1", "client_item_id": "i1"}
    out = stamp_raw_metadata(rm, audit)
    assert out["upload_audit"] == audit
    # Existing keys are preserved.
    assert out["format"] == "PNG"
    assert out["width"] == 1170


def test_stamp_handles_none_raw_metadata():
    """First-write path: raw_metadata may be None on the model when
    the upload route initializes the dict in the same constructor."""
    out = stamp_raw_metadata(None, {"client_batch_id": "b1"})
    assert out == {"upload_audit": {"client_batch_id": "b1"}}


def test_stamp_does_not_mutate_input_dict():
    """The caller may have a reference to the dict — stamping must
    produce a new dict, not mutate in place."""
    rm = {"format": "PNG"}
    stamp_raw_metadata(rm, {"client_batch_id": "b1"})
    assert "upload_audit" not in rm
