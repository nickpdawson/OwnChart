"""Ingest resource boundary — the single enforcement point for upload caps.

P0-5 (self-hosted/hosted release review, 2026-07-20): multiple upload and
extraction handlers read the whole body into memory with no effective byte,
page, pixel or file-count cap, and the documented ``ingest.max_attachment_bytes``
/ ``ingest.max_pdf_pages`` YAML values were parsed but never enforced.

This module makes those YAML values *live*. Every limit is read from
``get_app_config().ingest`` (config.yaml ``ingest:`` block → enforced), so
there is exactly one place an operator changes a cap and one place the code
reads it.

What it provides:

  - ``reject_if_declared_length_over`` — fast Content-Length reject before we
    stream anything (never trusted alone; the streaming counter is authoritative).
  - ``read_upload_capped`` / ``read_body_capped`` — chunked reads with a running
    byte counter that abort at the limit with a stable 413 code.
  - ``verify_upload_signature`` — magic-byte sniff; declared type must match the
    real bytes for pdf/png/jpeg/gif/tiff/webp/heic, else stable 422.
  - ``enforce_file_count`` — file-count cap for multi-file endpoints.
  - ``PixelLimitExceeded`` + ``max_image_pixels`` — decompression-bomb guard,
    consumed by ``ingest/images.py``.

Error responses use a structured ``detail`` dict ``{"code", "message", ...}``
— the same wire shape the auth perimeter already returns (see
``core/auth_context``). Stable ``code`` values are the machine contract; the
``message`` is human-facing. No parser exception text and no filenames ever
land in ``detail`` (PHI-safe; P1-13 adjacency).
"""

from __future__ import annotations

from fastapi import HTTPException, Request, UploadFile, status

from ..core.app_config import get_app_config

# Chunk size for streaming reads. 1 MiB balances syscall count against the
# resident-memory ceiling while a capped read is in flight.
_CHUNK_BYTES = 1024 * 1024

# Content-Length is a coarse pre-check. For multipart bodies it includes
# boundary/header overhead on top of the file bytes, so a file of exactly
# ``max_attachment_bytes`` can report a slightly larger Content-Length. Allow
# this slack on the *declared* length only; the streaming counter still
# enforces the exact per-file cap on the real bytes.
_CL_SLACK_BYTES = 1024 * 1024


# --- Stable public error codes -------------------------------------------------
CODE_TOO_LARGE = "upload_too_large"
CODE_UNSUPPORTED_MEDIA = "unsupported_media_type"
CODE_IMAGE_TOO_LARGE = "image_too_large"
CODE_TOO_MANY_FILES = "too_many_files"
CODE_UNPROCESSABLE = "unprocessable_upload"


class PixelLimitExceeded(Exception):
    """Raised by the image lane when an image's pixel count exceeds
    ``ingest.max_image_pixels`` (decompression-bomb guard). Carries the
    offending and allowed counts so the handler can build a stable 422."""

    def __init__(self, pixels: int, max_pixels: int) -> None:
        self.pixels = pixels
        self.max_pixels = max_pixels
        super().__init__(f"image {pixels}px exceeds {max_pixels}px cap")


# --- Config accessors (one read point) ----------------------------------------
def max_attachment_bytes() -> int:
    return get_app_config().ingest.max_attachment_bytes


def max_pdf_pages() -> int:
    return get_app_config().ingest.max_pdf_pages


def max_image_pixels() -> int:
    return get_app_config().ingest.max_image_pixels


def max_files_per_request() -> int:
    return get_app_config().ingest.max_files_per_request


# --- Rejection helper ----------------------------------------------------------
def _reject(status_code: int, code: str, message: str, **extra: object) -> None:
    detail: dict[str, object] = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status_code, detail=detail)


def _reject_too_large(max_bytes: int) -> None:
    _reject(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        CODE_TOO_LARGE,
        f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit.",
        max_bytes=max_bytes,
    )


# --- Content-Length fast reject ------------------------------------------------
def reject_if_declared_length_over(request: Request, max_bytes: int) -> None:
    """Reject grossly-oversized uploads before streaming a single byte.

    Coarse by design: a lying or absent Content-Length is caught later by the
    streaming counter, which is the authoritative gate. This only short-circuits
    the honest multi-hundred-MB case so we never buffer it.
    """
    raw = request.headers.get("content-length")
    if not raw:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared > max_bytes + _CL_SLACK_BYTES:
        _reject_too_large(max_bytes)


# --- Streaming byte-capped reads ----------------------------------------------
async def read_upload_capped(file: UploadFile, max_bytes: int | None = None) -> bytes:
    """Read an ``UploadFile`` in chunks, aborting at ``max_bytes`` with a 413.

    Bounds the resident-memory footprint of the read (a bare ``await
    file.read()`` materialises the entire part in one allocation) and rejects
    oversized uploads before any parse / render / OCR / LLM work begins.
    """
    limit = max_attachment_bytes() if max_bytes is None else max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            await file.close()
            _reject_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_body_capped(request: Request, max_bytes: int | None = None) -> bytes:
    """Read a raw request body via ``request.stream()`` with a running counter.

    Unlike ``await request.body()`` (which buffers the whole body first), this
    aborts mid-stream at ``max_bytes`` so a runaway push never fully lands.
    """
    limit = max_attachment_bytes() if max_bytes is None else max_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            _reject_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


# --- File-count cap ------------------------------------------------------------
def enforce_file_count(files: list[UploadFile]) -> None:
    limit = max_files_per_request()
    if len(files) > limit:
        _reject(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            CODE_TOO_MANY_FILES,
            f"Too many files in one request (max {limit}).",
            max_files=limit,
        )


# --- Magic-byte signature verification ----------------------------------------
def sniff_kind(raw: bytes) -> str | None:
    """Best-effort container sniff from leading bytes. stdlib only, no new deps.

    Returns a coarse kind: 'pdf' | 'png' | 'jpeg' | 'gif' | 'tiff' | 'webp'
    | 'heif' | 'xml' | 'json' | None.
    """
    if len(raw) < 12:
        # Very short bodies can still be valid JSON/XML fragments; sniff those
        # from what we have, otherwise unknown.
        head = raw.lstrip()[:1]
        if head == b"{" or head == b"[":
            return "json"
        return None
    if raw[:5] == b"%PDF-":
        return "pdf"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    # ISO-BMFF (HEIC/HEIF): 'ftyp' box at offset 4, brand in bytes 8-12.
    if raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis",
                     b"hevm", b"hevs", b"mif1", b"msf1"):
            return "heif"
        return "heif"  # other ftyp brands (mp4 etc.) still 'container'
    stripped = raw.lstrip()
    if stripped[:5].lower() == b"<?xml" or stripped[:1] == b"<":
        return "xml"
    if stripped[:1] in (b"{", b"["):
        return "json"
    return None


# Declared content-type prefix/exact → the set of sniffed kinds that satisfy it.
_IMAGE_KIND_FOR_CT = {
    "image/jpeg": {"jpeg"},
    "image/png": {"png"},
    "image/gif": {"gif"},
    "image/tiff": {"tiff"},
    "image/heic": {"heif"},
    "image/heif": {"heif"},
    "image/webp": {"webp"},
}


def verify_upload_signature(raw: bytes, declared_content_type: str | None, *, kind: str) -> None:
    """Verify the real bytes match the declared/expected container.

    ``kind`` is the lane's expectation: 'image' | 'pdf'. A mismatch (declared
    PNG but bytes are a PDF, declared PDF but bytes are HTML, …) is rejected as
    422 ``unsupported_media_type`` before any parser touches the bytes. Kept
    lightweight and lenient: an *unknown* sniff for an otherwise plausible body
    is allowed through (the parser is the final authority); only a *positive
    mismatch* is rejected.
    """
    sniffed = sniff_kind(raw)
    if kind == "pdf":
        if sniffed is not None and sniffed != "pdf":
            _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                CODE_UNSUPPORTED_MEDIA,
                "File content does not match a PDF.",
            )
        return
    if kind == "image":
        # If the client declared a specific image type, the bytes must match it
        # when we can sniff them. If we can't sniff (e.g. an exotic-but-valid
        # HEIC variant), defer to the decoder.
        if sniffed is None:
            return
        if sniffed not in {"png", "jpeg", "gif", "tiff", "webp", "heif"}:
            _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                CODE_UNSUPPORTED_MEDIA,
                "File content is not a recognised image.",
            )
        expected = _IMAGE_KIND_FOR_CT.get((declared_content_type or "").lower())
        if expected is not None and sniffed not in expected:
            _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                CODE_UNSUPPORTED_MEDIA,
                "File content does not match its declared image type.",
            )
        return


def reject_unprocessable(message: str) -> None:
    """Stable 422 for a body that fails to parse. Callers pass a *generic*
    message — never the parser exception text or a filename (PHI-safe)."""
    _reject(status.HTTP_422_UNPROCESSABLE_ENTITY, CODE_UNPROCESSABLE, message)
