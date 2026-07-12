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
import ipaddress
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..core.logger import get_logger
from .fhir import REQUEST_TIMEOUT_S
from .text_sanitize import strip_nul

log = get_logger("ownchart.ingest.fhir_attachments")

# Hard cap on one fetched attachment. A hostile / misconfigured EHR
# (or an SSRF target) must not be able to stream an unbounded body into
# the worker's memory. 25 MB comfortably covers real op-note PDFs.
MAX_ATTACHMENT_BYTES: int = 25 * 1024 * 1024

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


class AttachmentUrlError(ValueError):
    """Raised when an attachment URL fails the SSRF / scheme checks.

    The reason string is a stable code (never the full URL) so the
    caller can log it without echoing an operator-controlled internal
    target back into a response or a log.
    """


def _host_port(parts) -> tuple[str | None, int | None]:
    """(hostname, port) for a urlsplit result, port defaulted by scheme."""
    host = parts.hostname
    port = parts.port
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return host, port


def _assert_public_host(host: str) -> None:
    """Resolve `host` and reject any private / loopback / link-local /
    multicast / reserved / metadata destination — the SSRF guard.

    Mirrors the resolved-IP checks in ``ingest/calendar_ics.py``
    (kept as a separate copy so the two threat models can diverge and
    so neither import couples to the other).
    """
    try:
        resolved = socket.gethostbyname(host)
    except OSError:
        raise AttachmentUrlError("attachment_host_unresolvable") from None
    try:
        ip = ipaddress.ip_address(resolved)
    except ValueError:
        raise AttachmentUrlError("attachment_resolved_ip_unparseable") from None
    if ip.is_loopback:
        raise AttachmentUrlError("attachment_resolves_to_loopback")
    if ip.is_link_local:
        raise AttachmentUrlError("attachment_resolves_to_link_local")
    if ip.is_multicast:
        raise AttachmentUrlError("attachment_resolves_to_multicast")
    if ip.is_reserved:
        raise AttachmentUrlError("attachment_resolves_to_reserved")
    if ip.is_unspecified:
        raise AttachmentUrlError("attachment_resolves_to_unspecified")
    if ip.is_private:
        raise AttachmentUrlError("attachment_resolves_to_private_range")
    if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
        raise AttachmentUrlError("attachment_resolves_to_metadata_service")


@dataclass(frozen=True)
class ResolvedAttachmentUrl:
    """Output of ``resolve_attachment_url`` — the URL to fetch plus
    whether it is same-origin as the FHIR base (i.e. whether the EHR
    bearer token may be sent with the request)."""

    url: str
    same_host_as_base: bool


def resolve_attachment_url(fhir_base: str, url: str) -> ResolvedAttachmentUrl:
    """Resolve an attachment `url` (relative ``Binary/<id>`` or absolute)
    against `fhir_base` and enforce the SSRF policy.

    Rules:
      - A relative reference is joined onto ``fhir_base``; it inherits
        the base host and IS same-origin (token allowed).
      - An absolute URL must be https, OR http only when it is exactly
        same-origin (scheme+host+port) as ``fhir_base`` (some sandbox
        FHIR servers are plain-http). Its host must resolve to a public
        IP (``_assert_public_host``).
      - The bearer token is only sent when the fetch host == the
        ``fhir_base`` host. A tampered ``attachment.url`` pointing at a
        third party never receives the EHR token.

    Raises ``AttachmentUrlError`` on any violation.
    """
    base_parts = urlsplit(fhir_base.strip())
    base_host, base_port = _host_port(base_parts)
    if not base_host:
        raise AttachmentUrlError("fhir_base_host_missing")

    if not url or not isinstance(url, str):
        raise AttachmentUrlError("attachment_url_empty")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if not scheme:
        # Relative reference (Binary/<id>) — same origin as the base by
        # construction. The base host was already vetted when the
        # operator bound the connector. A URL carrying ANY scheme (even a
        # non-network one like file:/gopher:) is NOT relative; it falls
        # through to the scheme allowlist below and is rejected.
        joined = f"{fhir_base.rstrip('/')}/{url.strip().lstrip('/')}"
        return ResolvedAttachmentUrl(url=joined, same_host_as_base=True)

    host, port = _host_port(parts)
    if not host:
        raise AttachmentUrlError("attachment_url_host_missing")

    same_origin = (
        host.lower() == base_host.lower()
        and port == base_port
        and scheme == base_parts.scheme.lower()
    )

    if scheme == "http" and not same_origin:
        # http is only tolerated when it is the same plain-http origin
        # as the FHIR base; an arbitrary http:// attachment is rejected.
        raise AttachmentUrlError("attachment_url_insecure_cross_origin")
    if scheme not in ("http", "https"):
        raise AttachmentUrlError("attachment_url_bad_scheme")

    _assert_public_host(host)

    same_host = host.lower() == base_host.lower()
    return ResolvedAttachmentUrl(url=url, same_host_as_base=same_host)


def _too_large(r: httpx.Response) -> bool:
    """True if the response body exceeds MAX_ATTACHMENT_BYTES.

    Checks the declared Content-Length first (cheap), then the actual
    materialized body length (httpx has already read it in the
    non-streaming path, so this is the authoritative check)."""
    declared = r.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_ATTACHMENT_BYTES:
                return True
        except ValueError:
            pass
    return len(r.content) > MAX_ATTACHMENT_BYTES


async def _fetch_one(
    client: httpx.AsyncClient,
    fhir_base: str,
    access_token: str,
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

    # SSRF guard: validate scheme + resolve the host to a public IP, and
    # decide whether the EHR bearer token may be sent. A tampered
    # DocumentReference.attachment.url pointing at an internal host
    # (e.g. http://10.15.x.x/) is rejected before any request goes out,
    # and even a permitted cross-host URL never receives the token.
    try:
        resolved = resolve_attachment_url(fhir_base, ref.url)
    except AttachmentUrlError as e:
        return FetchedAttachment(
            ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None,
            error=f"url rejected: {e}",
        )
    url = resolved.url
    # Only send the EHR bearer token to the FHIR base host itself.
    auth_headers: dict[str, str] = (
        {"Authorization": f"Bearer {access_token}"}
        if resolved.same_host_as_base else {}
    )
    try:
        # Ask for the FHIR Binary wrapper first (more portable across vendors).
        r = await client.get(
            url, headers={"Accept": "application/fhir+json", **auth_headers},
        )
        if not r.is_success:
            # Some Epic instances respond 406; retry as raw bytes
            r = await client.get(
                url, headers={"Accept": ref.content_type or "*/*", **auth_headers},
            )
        if not r.is_success:
            return FetchedAttachment(
                ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None,
                error=f"HTTP {r.status_code}",
            )
        if _too_large(r):
            return FetchedAttachment(
                ref=ref, bytes_=b"", mime=ref.content_type or "", plaintext=None,
                error=f"attachment exceeds {MAX_ATTACHMENT_BYTES} byte cap",
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

    # No client-level Authorization: the bearer token is attached
    # per-request inside `_fetch_one` ONLY when the resolved fetch host
    # matches the FHIR base host (SSRF token-leak guard). follow_redirects
    # is off so a 302 can't bounce us to a private host after the
    # resolved-IP check already passed.
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=False,
    ) as client:

        async def run(r: AttachmentRef) -> FetchedAttachment:
            async with sem:
                return await _fetch_one(client, fhir_base, access_token, r)

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
    # `errors="replace"` fixes invalid UTF-8 but does NOT strip embedded
    # NUL bytes that were valid in the source. Those crash the JSONB /
    # text write in connectors.py (raw_metadata.plaintext_excerpt +
    # EvidenceAnchor.text_excerpt) and roll back the whole sync. Strip
    # here so every consumer of att.plaintext is NUL-free.
    text = strip_nul(text)
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
    # `title` / `creation` come from operator-uncontrolled FHIR resource
    # fields (DocumentReference.description, .date) and can carry an
    # embedded NUL (a literal 0x00 in the source JSON). This dict is
    # written to raw_metadata JSONB, so sanitize every value.
    return strip_nul({
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
    })
