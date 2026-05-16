"""Image ingestion lane.

Supports JPEG, PNG, GIF, TIFF (native Pillow) and HEIC (pillow-heif).
For each upload we:

  1. Detect the image's true type by magic bytes + Pillow's identification.
  2. Read EXIF — surface DateTimeOriginal as captured_at; preserve the full
     dict (incl. GPS) in exif_metadata. GPS redaction is a V1.1 follow-up;
     for V1 we record it and let the user remove later.
  3. Generate small + medium WebP thumbnails written under
     {DATA_DIR}/renders/{source_id}/thumb-sm.webp and thumb-md.webp.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image
from pillow_heif import register_heif_opener

from ..core.config import get_settings
from ..core.logger import get_logger

register_heif_opener()  # idempotent

log = get_logger("ownchart.ingest.images")

SUPPORTED_MIME = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/tiff",
    "image/heic",
    "image/heif",
    "image/webp",
}

SUPPORTED_FORMATS = {"JPEG", "PNG", "GIF", "TIFF", "HEIF", "WEBP"}

THUMB_SIZES = {
    "sm": 240,
    "md": 720,
}


@dataclass
class ImageMetadata:
    pil_format: str
    width: int
    height: int
    captured_at: datetime | None
    exif: dict[str, Any] = field(default_factory=dict)
    has_gps: bool = False
    thumbnails: dict[str, str] = field(default_factory=dict)  # size_key -> storage_uri


def _renders_dir(source_id: str) -> Path:
    return get_settings().data_dir / "renders" / source_id


def _decode_exif(img: Image.Image) -> tuple[dict[str, Any], bool]:
    raw = getattr(img, "_getexif", lambda: None)() or {}
    if not raw:
        return {}, False
    decoded: dict[str, Any] = {}
    has_gps = False
    for tag_id, value in raw.items():
        tag = ExifTags.TAGS.get(tag_id, str(tag_id))
        if tag == "GPSInfo":
            has_gps = True
            gps = {}
            for gps_id, gps_val in (value or {}).items():
                gps[ExifTags.GPSTAGS.get(gps_id, str(gps_id))] = _safe(gps_val)
            decoded["GPSInfo"] = gps
        else:
            decoded[tag] = _safe(value)
    return decoded, has_gps


def _safe(v: Any) -> Any:
    """JSON-friendly coercion (Pillow can return IFDRational, bytes, tuples).

    Postgres `text`/`jsonb` cannot store NULL bytes (`\\x00`). iOS PNG
    screenshots' EXIF `UserComment` tag follows the spec
    `"ASCII\\x00\\x00\\x00<text>"` (8-byte charset header), so the
    nulls land in the *middle* of the decoded string — `.rstrip` won't
    save us. Strip every `\\x00` from all decoded strings on the way
    out. Caught the alpha-day photo-upload P0 on 2026-05-16:
    `asyncpg.exceptions.UntranslatableCharacterError: \\u0000 cannot be
    converted to text` when writing `source_documents.exif_metadata`.
    """
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace").replace("\x00", "")
        except Exception:  # noqa: BLE001
            return v.hex()
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, (tuple, list)):
        return [_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k).replace("\x00", ""): _safe(x) for k, x in v.items()}
    if hasattr(v, "numerator") and hasattr(v, "denominator"):
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            return f"{v.numerator}/{v.denominator}"
    return v


def _parse_exif_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    # EXIF DateTimeOriginal format: "YYYY:MM:DD HH:MM:SS"
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def analyze_and_thumbnail(blob_bytes: bytes, source_id: str) -> ImageMetadata:
    """Inspect an image's bytes, write thumbnails, return metadata.

    Caller is responsible for persisting the SourceDocument row and
    setting captured_at + exif_metadata from the returned object.
    """
    with Image.open(io.BytesIO(blob_bytes)) as img:
        if img.format not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported image format: {img.format!r}")

        # Honor EXIF orientation when generating thumbnails.
        try:
            from PIL import ImageOps

            oriented = ImageOps.exif_transpose(img)
        except Exception:  # noqa: BLE001
            oriented = img

        exif, has_gps = _decode_exif(img)
        captured_at = _parse_exif_datetime(exif.get("DateTimeOriginal") or exif.get("DateTime"))

        out_dir = _renders_dir(source_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        thumbs: dict[str, str] = {}
        for key, max_side in THUMB_SIZES.items():
            thumb = oriented.copy()
            thumb.thumbnail((max_side, max_side))
            # Convert to RGB for WebP if image has alpha-incompatible modes.
            if thumb.mode not in ("RGB", "RGBA"):
                thumb = thumb.convert("RGB")
            out_path = out_dir / f"thumb-{key}.webp"
            thumb.save(out_path, format="WEBP", quality=82, method=6)
            thumbs[key] = str(out_path)

        return ImageMetadata(
            pil_format=img.format,
            width=img.width,
            height=img.height,
            captured_at=captured_at,
            exif=exif,
            has_gps=has_gps,
            thumbnails=thumbs,
        )
