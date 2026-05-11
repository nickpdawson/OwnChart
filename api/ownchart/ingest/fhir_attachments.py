"""Walk a FHIR snapshot and download attachments.

Looks at:
  - DocumentReference.content[].attachment   (clinical notes, op notes,
                                              discharge summaries, faxes)
  - DiagnosticReport.presentedForm[]         (lab/radiology reports as PDF/RTF)

For each attachment, the binary is fetched (with the connection's bearer
token), persisted as its own SourceDocument (so the existing render +
OCR + Vision pipelines work uniformly), and linked back to the FHIR
resource that referenced it.

Best-effort plaintext extraction handles HTML / XML / RTF / plain text
inline so a quick text excerpt can be stored on the SourceDocument's
raw_metadata. Borrowed shape from jmandel/health-skillz (MIT).
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ..core.logger import get_logger
from .fhir import REQUEST_TIMEOUT_S

log = get_logger("ownchart.ingest.fhir_attachments")

# Mime types we'll attempt plaintext extraction on inline. Anything else
# becomes a SourceDocument with raw bytes only — the user can run OCR /
# vision on it later from /sources/{id}.
_TEXT_MIMES = {
    "text/plain",
    "text/html",
    "application/xml",
    "text/xml",
    "application/xhtml+xml",
    "application/rtf",
    "text/rtf",
    "application/json",
}

# Mime → file extension for vault storage. Falls through to .bin for
# unknown types.
_MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/gif": ".gif",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/xhtml+xml": ".xhtml",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "application/json": ".json",
    "application/cda+xml": ".xml",
    "application/dicom": ".dcm",
}


@dataclass
class AttachmentRef:
    """A normalized attachment pointer collected from a FHIR resource."""

    source_resource_type: str         # "DocumentReference" | "DiagnosticReport"
    source_resource_id: str
    content_index: int                # which entry in content[] / presentedForm[]
    content_type: str | None
    url: str | None                   # Binary/<id> or absolute URL
    inline_b64: str | None            # if attachment.data was set
    title: str | None                 # DocumentReference.description / type.text
    creation: str | None              # DocumentReference.date / DiagnosticReport.issued


@dataclass
class FetchedAttachment:
    ref: AttachmentRef
    bytes_: bytes
    mime: str
    plaintext: str | None
    error: str | None = None


@dataclass
class AttachmentSummary:
    fetched: list[FetchedAttachment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.fetched)


# ---------------------------------------------------------------------------
# Collection — pull all attachment refs out of the snapshot
# ---------------------------------------------------------------------------


def collect_attachment_refs(snap_fhir: dict[str, list[dict]]) -> list[AttachmentRef]:
    refs: list[AttachmentRef] = []

    for doc in snap_fhir.get("DocumentReference", []) or []:
        rid = doc.get("id") or "?"
        title = (doc.get("type") or {}).get("text") or doc.get("description")
        creation = doc.get("date")
        for i, content in enumerate(doc.get("content", []) or []):
            att = content.get("attachment") or {}
            url = att.get("url")
            data = att.get("data")
            if not url and not data:
                continue
            refs.append(
                AttachmentRef(
                    source_resource_type="DocumentReference",
                    source_resource_id=rid,
                    content_index=i,
                    content_type=att.get("contentType"),
                    url=url,
                    inline_b64=data,
                    title=title,
                    creation=creation,
                )
            )

    for rep in snap_fhir.get("DiagnosticReport", []) or []:
        rid = rep.get("id") or "?"
        title = (rep.get("code") or {}).get("text") or "Diagnostic report"
        creation = rep.get("issued") or rep.get("effectiveDateTime")
        for i, att in enumerate(rep.get("presentedForm", []) or []):
            url = att.get("url")
            data = att.get("data")
            if not url and not data:
                continue
            refs.append(
                AttachmentRef(
                    source_resource_type="DiagnosticReport",
                    source_resource_id=rid,
                    content_index=i,
                    content_type=att.get("contentType"),
                    url=url,
                    inline_b64=data,
                    title=title,
                    creation=creation,
                )
            )

    log.info("attachment_refs_collected", count=len(refs))
    return refs


# ---------------------------------------------------------------------------
# Fetch — download each attachment
# ---------------------------------------------------------------------------


def _abs_url(fhir_base: str, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    base = fhir_base.rstrip("/")
    return f"{base}/{url.lstrip('/')}"


async def _fetch_one(
    client: httpx.AsyncClient,
    fhir_base: str,
    ref: AttachmentRef,
) -> FetchedAttachment:
    """Resolve and download one attachment. Inline data short-circuits the network."""
    if ref.inline_b64:
        try:
            raw = base64.b64decode(ref.inline_b64)
            mime = ref.content_type or "application/octet-stream"
            return FetchedAttachment(
                ref=ref, bytes_=raw, mime=mime,
                plaintext=_extract_plaintext(raw, mime),
            )
        except Exception as e:  # noqa: BLE001
            return FetchedAttachment(ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None, error=str(e))

    if not ref.url:
        return FetchedAttachment(ref=ref, bytes_=b"", mime="", plaintext=None, error="no url and no inline data")

    url = _abs_url(fhir_base, ref.url)
    try:
        # Ask for the FHIR Binary wrapper first (more portable across vendors).
        r = await client.get(url, headers={"Accept": "application/fhir+json"})
        if not r.is_success:
            # Some Epic instances respond 406; retry as raw bytes
            r = await client.get(url, headers={"Accept": ref.content_type or "*/*"})
        if not r.is_success:
            return FetchedAttachment(
                ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None,
                error=f"HTTP {r.status_code}",
            )
        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct.endswith("/fhir+json") or ct == "application/json":
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("resourceType") == "Binary" and body.get("data"):
                raw = base64.b64decode(body["data"])
                mime = body.get("contentType") or ref.content_type or "application/octet-stream"
                return FetchedAttachment(
                    ref=ref, bytes_=raw, mime=mime,
                    plaintext=_extract_plaintext(raw, mime),
                )
            # Some servers return the binary as a JSON wrapper without `data`.
            return FetchedAttachment(
                ref=ref, bytes_=r.content, mime=ref.content_type or ct,
                plaintext=_extract_plaintext(r.content, ref.content_type or ct),
            )
        # Raw bytes path
        mime = ct or ref.content_type or "application/octet-stream"
        return FetchedAttachment(
            ref=ref, bytes_=r.content, mime=mime,
            plaintext=_extract_plaintext(r.content, mime),
        )
    except Exception as e:  # noqa: BLE001
        return FetchedAttachment(ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None, error=str(e))


async def fetch_attachments(
    *,
    fhir_base: str,
    access_token: str,
    refs: list[AttachmentRef],
    concurrency: int = 4,
    max_attachments: int = 200,
) -> AttachmentSummary:
    if not refs:
        return AttachmentSummary()
    out = AttachmentSummary()
    refs = refs[:max_attachments]
    import asyncio
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as client:

        async def run(r: AttachmentRef) -> FetchedAttachment:
            async with sem:
                return await _fetch_one(client, fhir_base, r)

        results = await asyncio.gather(*[run(r) for r in refs])

    for r in results:
        if r.error:
            out.errors.append(f"{r.ref.source_resource_type}/{r.ref.source_resource_id}#{r.ref.content_index}: {r.error}")
        if r.bytes_:
            out.fetched.append(r)
    log.info(
        "attachments_fetched",
        attempted=len(refs),
        ok=len(out.fetched),
        errors=len(out.errors),
    )
    return out


# ---------------------------------------------------------------------------
# Best-effort plaintext extraction
# ---------------------------------------------------------------------------


_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")


def _extract_plaintext(raw: bytes, mime: str | None) -> str | None:
    """Return a UTF-8 plaintext rendering of `raw` if it's a text-ish format.

    PDF / DICOM / images return None — those flow through OCR / Vision later.
    """
    if not raw:
        return None
    m = (mime or "").lower().split(";")[0].strip()
    if m == "application/pdf":
        return None
    if m.startswith("image/") or m == "application/dicom":
        return None
    if m not in _TEXT_MIMES and not m.startswith("text/"):
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    if "html" in m or "xml" in m or "xhtml" in m:
        # CCDA narrative / HTML — strip tags
        text = _RE_TAG.sub(" ", text)
    elif "rtf" in m:
        # Crude RTF strip — control words and braces; won't render tables
        # cleanly but salvages most prose. Full rendering needs `rtf.js` or
        # similar (deferred).
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
        text = text.replace("{", "").replace("}", "")
    text = _RE_WS.sub(" ", text).strip()
    return text or None


def ext_for_mime(mime: str | None, fallback: str = ".bin") -> str:
    if not mime:
        return fallback
    return _MIME_TO_EXT.get(mime.split(";")[0].strip().lower(), fallback)


def derive_source_type(mime: str | None) -> str:
    if not mime:
        return "fhir_attachment"
    m = mime.lower().split(";")[0].strip()
    if m == "application/pdf":
        return "pdf"             # reuse the existing PDF render pipeline
    if m.startswith("image/"):
        return "photo"           # reuse image thumb pipeline
    if "xml" in m or m == "text/xml" or m == "application/cda+xml":
        return "ccda_xml"        # reuse CCDA parser if structure permits
    return "clinical_note"


def summary_for_metadata(att: FetchedAttachment, parent_connection_id: uuid.UUID | None) -> dict[str, Any]:
    return {
        "fhir_resource_type": att.ref.source_resource_type,
        "fhir_resource_id": att.ref.source_resource_id,
        "content_index": att.ref.content_index,
        "title": att.ref.title,
        "creation": att.ref.creation,
        "content_type": att.mime,
        "size_bytes": len(att.bytes_),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "parent_connection_id": str(parent_connection_id) if parent_connection_id else None,
        "has_plaintext": att.plaintext is not None,
        "plaintext_length": len(att.plaintext) if att.plaintext else 0,
    }
