"""Calendar life-context retrieval for Ask (FU-CAL-ASK-INTEGRATION).

Pulls recent + upcoming calendar events for the active record and
projects them through ``project_event_for_llm()`` so the floor
defined in the Slice 3 doctrine holds:

  - Storage privacy mode controls what's on the row.
  - LLM consent (``calendar_sources.llm_full_details_consent``)
    controls what reaches the LLM context.
  - Per-source ``history_window_back`` is the OUTER clamp —
    events outside that window are never projected to Ask even if
    they exist on disk (narrowing the window hides events from
    Ask without hard-delete; widening triggers a backfill which is
    a worker-side concern).

This module is pure-ish: it takes an ``AsyncSession`` and returns
formatted lines + counts. No LLM call here; that lives in
``routes/ask.py``. Splitting it out keeps the projector contract
testable without spinning up the Ask route.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingest.calendar_eventkit import project_event_for_llm
from ..models.calendar_event import CalendarEvent
from ..models.calendar_source import CalendarSource


# Per-source history_window_back → back-window timedelta. ``all``
# uses a far-back sentinel so the SQL window query still works.
_HISTORY_DELTAS: dict[str, timedelta] = {
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
    "3y": timedelta(days=365 * 3),
    "5y": timedelta(days=365 * 5),
    "all": timedelta(days=365 * 50),
}
# Forward window for "next month" coverage; keeps Ask context bounded.
_DEFAULT_FORWARD = timedelta(days=30)


def history_window_back_to_delta(window: str) -> timedelta:
    """Public mapping used by the worker too. Returns the back-window
    delta for one of the five enum values; raises ValueError on an
    unknown input so a future widening can't silently degrade to a
    default."""
    if window not in _HISTORY_DELTAS:
        raise ValueError(f"unknown history_window_back: {window!r}")
    return _HISTORY_DELTAS[window]


async def fetch_calendar_life_context(
    db: AsyncSession,
    *,
    person_record_id: uuid.UUID,
    now: datetime | None = None,
    forward_window: timedelta = _DEFAULT_FORWARD,
    max_events: int = 50,
) -> list[dict[str, Any]]:
    """Return up to ``max_events`` projected calendar events for the
    active record, clipped by each owning source's
    ``history_window_back``. Events are ordered by ``start_at`` desc
    (most recent first) so the cap drops older events first.

    Each returned dict has:
      - ``event`` — the projected fields per
        ``project_event_for_llm`` (start/end/all_day always; title /
        location / notes / attendees_count when allowed).
      - ``source_display_name`` — for the LLM to ground its answer
        ("Apps (Personal)" vs "Work").
      - ``source_id`` — for downstream citation.
    """
    now = now or datetime.now(timezone.utc)
    forward_at = now + forward_window
    farthest_back = now - max(_HISTORY_DELTAS.values())

    # Join calendar_events → calendar_sources so we get both the per-
    # event payload AND the source's consent + history_window_back +
    # display name in one round-trip.
    stmt = (
        select(CalendarEvent, CalendarSource)
        .join(
            CalendarSource,
            CalendarSource.id == CalendarEvent.calendar_source_id,
        )
        .where(CalendarEvent.person_record_id == person_record_id)
        .where(CalendarEvent.tombstoned_at.is_(None))
        .where(CalendarSource.disconnected_at.is_(None))
        # SQL pre-clamp: skip rows clearly out of any window's back
        # range. Per-source clamp runs in Python below for clarity.
        .where(CalendarEvent.start_at >= farthest_back)
        .where(CalendarEvent.start_at <= forward_at)
        .order_by(CalendarEvent.start_at.desc())
        # Pull a bit more than max_events so per-source clamping doesn't
        # under-fill the result.
        .limit(max_events * 3)
    )
    rows = (await db.execute(stmt)).all()

    out: list[dict[str, Any]] = []
    for ev, src in rows:
        # Per-source clamp: events older than the source's
        # history_window_back are hidden from Ask even though they're
        # stored. Narrowing the window is a hide-not-delete signal.
        try:
            back_delta = history_window_back_to_delta(src.history_window_back)
        except ValueError:
            # Defensive: row has an unexpected window value. Skip.
            continue
        per_source_floor = now - back_delta
        if ev.start_at < per_source_floor:
            continue
        projection = project_event_for_llm(
            start_at=ev.start_at,
            end_at=ev.end_at,
            all_day=ev.all_day,
            title=ev.title,
            location=ev.location,
            notes=ev.notes,
            attendees_count=ev.attendees_count,
            privacy_mode_applied=ev.privacy_mode_applied,
            source_consent=bool(src.llm_full_details_consent),
        )
        out.append({
            "event": projection,
            "source_display_name": src.display_name,
            "source_id": str(src.id),
        })
        if len(out) >= max_events:
            break
    return out


def format_calendar_context_block(items: list[dict[str, Any]]) -> str:
    """Render the projected events as a context-block fragment the
    Ask prompt can consume. Mirrors ``_format_context`` in
    routes/ask.py — one line per event, terse, no PHI bleed from
    other records (the caller already scoped by person_record_id).

    Returns an empty string when ``items`` is empty so the caller
    can splice it in without conditional joining.
    """
    if not items:
        return ""
    lines = ["", "## Calendar context (recent and upcoming events)"]
    for it in items:
        ev = it["event"]
        date_str = ev["start_at"].date().isoformat() if ev.get("start_at") else "?"
        if ev.get("all_day"):
            time_str = "all-day"
        else:
            time_str = (
                ev["start_at"].strftime("%H:%M")
                + "–"
                + ev["end_at"].strftime("%H:%M")
            )
        # Title may be absent (busy_only or consent=false floor).
        title_part = ev.get("title")
        body = title_part if title_part else "(no title — privacy mode)"
        location_part = (
            f"  at {ev['location']}" if ev.get("location") else ""
        )
        lines.append(
            f"- {date_str} {time_str} · "
            f"calendar={it['source_display_name']}: {body}{location_part}"
        )
    return "\n".join(lines)
