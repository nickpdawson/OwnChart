"""Vendor FHIR endpoint directories — search before add.

V1 supports Epic only (Epic's open R4 endpoint list at
https://open.epic.com/Endpoints/R4 is public, comprehensive, and stable).
The directory is downloaded on first search, parsed, cached on disk under
{DATA_DIR}/directories/<vendor>.json with a 24-hour TTL, and searched by
substring + token similarity.

Athena's and Cerner's directories are deferred — for those vendors the
user pastes a fhir_base URL manually via the same Add-Connector form.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.config import get_settings
from ..core.logger import get_logger

log = get_logger("ownchart.ingest.provider_directory")


@dataclass
class DirectoryEntry:
    name: str
    fhir_base: str
    ehr_vendor: str


# Currently only Epic. Each value is the public source URL Epic publishes.
DIRECTORY_SOURCES: dict[str, str] = {
    "epic": "https://open.epic.com/Endpoints/R4",
}

CACHE_TTL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT = 30.0


def _cache_path(vendor: str) -> Path:
    return get_settings().data_dir / "directories" / f"{vendor}.json"


def _is_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    return (time.time() - p.stat().st_mtime) < CACHE_TTL_SECONDS


def _parse_epic_html(html: str) -> list[DirectoryEntry]:
    """Epic embeds the endpoint directory as a JSON-shaped chunk inside the
    HTML page. Use a forgiving name+address pair regex; the page is large
    (~640KB) but stable in shape."""
    out: list[DirectoryEntry] = []
    seen: set[tuple[str, str]] = set()
    # Each entry has both `"name": "<...>"` and a paired `"address": "<...>"`.
    # The address can come either before or after the name within the entry.
    for m in re.finditer(r'"name":\s*"([^"]+)"', html):
        name = m.group(1).strip()
        if not name:
            continue
        # Look at the following ~3000 chars for an "address" before another name.
        window = html[m.end():m.end() + 3000]
        # Find first address that occurs before the next 'name' boundary
        addr_m = re.search(r'"address":\s*"([^"]+)"', window)
        next_name_m = re.search(r'"name":\s*"', window)
        if addr_m and (next_name_m is None or addr_m.start() < next_name_m.start()):
            url = addr_m.group(1).strip()
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            out.append(DirectoryEntry(name=name, fhir_base=url, ehr_vendor="epic"))
    return out


async def _download(vendor: str) -> list[DirectoryEntry]:
    src = DIRECTORY_SOURCES.get(vendor)
    if not src:
        return []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        r = await c.get(src)
        r.raise_for_status()
        text = r.text
    if vendor == "epic":
        return _parse_epic_html(text)
    return []


def _serialize(entries: list[DirectoryEntry]) -> dict:
    return {
        "fetched_at": int(time.time()),
        "count": len(entries),
        "entries": [{"name": e.name, "fhir_base": e.fhir_base, "ehr_vendor": e.ehr_vendor} for e in entries],
    }


def _deserialize(blob: dict) -> list[DirectoryEntry]:
    return [DirectoryEntry(**e) for e in blob.get("entries", []) or []]


async def get_directory(vendor: str, force_refresh: bool = False) -> list[DirectoryEntry]:
    cache = _cache_path(vendor)
    if not force_refresh and _is_fresh(cache):
        try:
            return _deserialize(json.loads(cache.read_text()))
        except Exception:  # noqa: BLE001
            pass
    entries = await _download(vendor)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(_serialize(entries)))
    log.info("provider_directory_refreshed", vendor=vendor, count=len(entries))
    return entries


def search(entries: list[DirectoryEntry], query: str, limit: int = 25) -> list[DirectoryEntry]:
    if not query.strip():
        return entries[:limit]
    q = query.strip().lower()
    tokens = [t for t in re.split(r"\s+", q) if t]
    scored: list[tuple[int, DirectoryEntry]] = []
    for e in entries:
        hay = f"{e.name} {e.fhir_base}".lower()
        # Score: number of tokens hit, plus bonus for phrase match
        score = sum(1 for t in tokens if t in hay)
        if score == 0:
            continue
        if q in hay:
            score += 5
        scored.append((score, e))
    scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
    return [e for _, e in scored[:limit]]
