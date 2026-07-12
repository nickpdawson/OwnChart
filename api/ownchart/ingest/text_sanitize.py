"""Shared NUL-byte sanitizer for text/metadata destined for Postgres.

Postgres `text`/`jsonb` columns cannot store the NUL byte (`\\x00`);
asyncpg raises `UntranslatableCharacterError: \\u0000 cannot be
converted to text` and rolls back the whole transaction. Several
ingest lanes decode untrusted bytes with `errors="replace"` — which
handles invalid UTF-8 but does NOT strip embedded NULs that were
already valid in the source (iPhone EXIF `UserComment`, PDF text
layers / document metadata, FHIR DocumentReference narrative with an
embedded `\\u0000`). Every such value must pass through `strip_nul`
before it is written to a JSONB column or a `*_excerpt` text column.

This is the general-purpose analog of `ingest/images.py::_safe`
(which additionally coerces Pillow's IFDRational/bytes types). Use
`_safe` on the EXIF path; use `strip_nul` everywhere else.
"""

from __future__ import annotations

from typing import Any


def strip_nul(value: Any) -> Any:
    """Recursively remove NUL bytes so `value` is safe for Postgres
    text/jsonb.

    - `str`   → NULs removed.
    - `bytes` → NULs removed (stays bytes).
    - `dict`  → keys and values recursively sanitized.
    - `list` / `tuple` → each element recursively sanitized (returns a
      list; tuples are not JSON-representable anyway).
    - everything else (int, float, bool, None) → returned unchanged.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, bytes):
        return value.replace(b"\x00", b"")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value
