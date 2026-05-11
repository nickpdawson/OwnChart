"""Content-addressed evidence vault.

Every uploaded raw byte stream is written to:
    {DATA_DIR}/evidence/{sha[:2]}/{sha[2:4]}/{sha}{ext}

Two-level prefix avoids ext4/zfs single-directory pressure once the vault
holds tens of thousands of files. Original bytes are written exactly once;
duplicate uploads (same SHA) are detected and reused.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ..core.config import get_settings


@dataclass
class StoredBlob:
    sha256: str
    storage_uri: str          # absolute path, e.g. /data/evidence/ab/cd/abcd...jpg
    size_bytes: int
    already_existed: bool


def _evidence_dir() -> Path:
    return get_settings().data_dir / "evidence"


def _path_for(sha256: str, suffix: str = "") -> Path:
    base = _evidence_dir() / sha256[:2] / sha256[2:4]
    return base / f"{sha256}{suffix}"


async def write_blob(stream: AsyncIterator[bytes], suffix: str = "") -> StoredBlob:
    """Stream bytes to a temp file, hash as we go, then rename to the
    content-addressed path. Returns metadata about what landed."""
    base = _evidence_dir()
    base.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    size = 0

    # Two-stage write: tmp first, then rename. Rename is atomic on local fs.
    tmp_path = base / f".inflight-{id(stream)}-{suffix.lstrip('.')}"
    try:
        with tmp_path.open("wb") as f:
            async for chunk in stream:
                if not chunk:
                    continue
                h.update(chunk)
                f.write(chunk)
                size += len(chunk)
        sha = h.hexdigest()
        final = _path_for(sha, suffix)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            tmp_path.unlink(missing_ok=True)
            return StoredBlob(
                sha256=sha,
                storage_uri=str(final),
                size_bytes=final.stat().st_size,
                already_existed=True,
            )
        tmp_path.replace(final)
        return StoredBlob(
            sha256=sha,
            storage_uri=str(final),
            size_bytes=size,
            already_existed=False,
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def open_blob(sha256: str, suffix: str = "") -> Path:
    return _path_for(sha256, suffix)
