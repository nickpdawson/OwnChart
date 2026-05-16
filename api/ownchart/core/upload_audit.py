"""Stateless upload-batch correlation for iOS.

When iOS posts a batch of files (camera-roll multi-pick, HK backfill
chunk, a CCDA package etc.), individual requests can succeed or fail
independently. iOS keeps a local `(client_item_id -> file)` map so it
can show "5 of 30 uploaded, 4 failed" without ambiguity — *but only
if the server echoes the IDs back in error responses* (the JSON body
otherwise carries no link from request to local file).

Contract:

  - Optional headers ``X-Client-Batch-Id`` and ``X-Client-Item-Id``
    on every upload request. Both are opaque strings (UUIDv4 from
    iOS is conventional but not enforced).
  - Every error response from the catch-all handler echoes these
    when present, so iOS can correlate a 4xx/5xx back to the file
    that produced it.
  - Upload routes that successfully create a SourceDocument stamp
    them on ``raw_metadata.upload_audit`` for forensic queries
    ("show me everything from batch X"). The success response
    already contains the new SourceDocument id, so iOS doesn't
    need them re-echoed there.
  - Structured logs include both fields so a ``grep`` against the
    batch id surfaces every server-side log line for the batch.

No DB table for V1 — alpha is single-user; forensic queries hit
``raw_metadata->'upload_audit'->>'client_batch_id'`` directly.
A dedicated table can come post-alpha if multi-tenant scaling needs
indexed lookup.
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, Request


# Header names. iOS must spell them exactly (HTTP header names are
# case-insensitive but the documented contract is X-Client-Batch-Id
# / X-Client-Item-Id so iOS source reads cleanly).
HEADER_BATCH_ID = "x-client-batch-id"
HEADER_ITEM_ID = "x-client-item-id"


def upload_audit_from_request(request: Request) -> dict[str, str] | None:
    """Pull batch/item IDs out of the raw request headers.

    Used by the catch-all error handler — it doesn't have access to
    a dependency-injected value because it runs *after* an exception.

    Returns ``None`` when neither header is set, so the caller can
    decide whether to add an ``upload_audit`` key to the JSON body
    at all (we don't want empty-{}'s polluting responses to UI calls).
    """
    h = request.headers
    batch = h.get(HEADER_BATCH_ID)
    item = h.get(HEADER_ITEM_ID)
    if not batch and not item:
        return None
    out: dict[str, str] = {}
    if batch:
        out["client_batch_id"] = batch[:128]
    if item:
        out["client_item_id"] = item[:128]
    return out


def upload_audit_dep(
    x_client_batch_id: str | None = Header(default=None, max_length=128),
    x_client_item_id: str | None = Header(default=None, max_length=128),
) -> dict[str, str] | None:
    """FastAPI dependency for upload routes.

    Returns the same shape as :func:`upload_audit_from_request` so the
    success-path stamp and the error-path echo are symmetric.
    """
    if not x_client_batch_id and not x_client_item_id:
        return None
    out: dict[str, str] = {}
    if x_client_batch_id:
        out["client_batch_id"] = x_client_batch_id
    if x_client_item_id:
        out["client_item_id"] = x_client_item_id
    return out


def stamp_raw_metadata(
    raw_metadata: dict[str, Any] | None,
    audit: dict[str, str] | None,
) -> dict[str, Any] | None:
    """Merge the audit dict into a SourceDocument's raw_metadata.

    No-op when audit is None — keeps the metadata blob clean for
    server-originated rows (FHIR sync, CCDA import) that have no
    client-side correlation.
    """
    if not audit:
        return raw_metadata
    merged = dict(raw_metadata or {})
    merged["upload_audit"] = audit
    return merged
