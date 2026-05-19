"""Slice 4 export mappers — canonical OwnChart JSON + human TXT.

Both mappers take a fully-built ``ExportSnapshot`` (pure data; no
DB) and return bytes ready to write to disk. Designed for
predictable testing: same input → byte-identical output (modulo
the explicit ``generated_at`` field on the snapshot, which the
caller controls).

  canonical_ownchart_json_mapper(snapshot) → JSON bytes
    Stable key ordering (sort_keys=True) + ISO-8601 datetimes. The
    output is the canonical re-importable shape; future Pictal /
    CCDA mappers will read FROM this same snapshot object, NOT
    from JSON bytes.

  human_readable_txt_mapper(snapshot) → UTF-8 text bytes
    Sectioned plain text suitable for printing or pasting into an
    email. Sections in fixed order: Header, Record, Sources,
    Facts (grouped by year), Calendar (sources + events). Empty
    sections render an explicit "(none)" line so the absence is
    visible at a glance.

Both functions are pure: same input, same output, no I/O.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime

from .snapshot import ExportSnapshot


# Patient/operator disclaimer baked into both mapper outputs.
# Slice 4 hardening (PM 2026-05-19, review finding #7).
#
# The TXT mapper renders this in a sectioned block right after the
# document header so anyone receiving the file in email / print
# sees it before reading content. The JSON mapper surfaces the
# same text under a top-level "disclaimer" key so the canonical
# format also carries the framing — a future re-import or tooling
# can preserve it.
#
# The four "NOT" lines are load-bearing per OwnChart doctrine
# ("No medical advice. No HIPAA protection by default. Not a
# medical device.") and the source-available license posture.
# Don't soften.
EXPORT_DISCLAIMER = (
    "This is a patient-readable summary of the data the owner of "
    "this OwnChart record has chosen to organize. It is NOT a "
    "medical record, NOT a legal document, NOT a clinical care "
    "recommendation, and is NOT covered by HIPAA absent a separate "
    "Business Associate Agreement with the operator of this "
    "OwnChart instance."
)


# ---------------------------------------------------------------------------
# JSON mapper


def _json_default(obj):
    """JSON serializer for the few types Pydantic dumps that
    ``json`` doesn't handle natively."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(
        f"json mapper got non-serializable type {type(obj).__name__}"
    )


def canonical_ownchart_json_mapper(snapshot: ExportSnapshot) -> bytes:
    """Serialize a snapshot as canonical OwnChart JSON bytes.

    Stable key ordering, 2-space indent, datetimes as ISO-8601.
    Suitable for diffing two snapshots, content-addressed storage,
    or re-import into a future OwnChart instance.

    Slice 4 hardening (PM 2026-05-19): the output carries a
    top-level ``disclaimer`` key with the same patient/non-medical
    framing the TXT mapper prints. A future re-import or tooling
    preserves the framing alongside the data.

    Pure function — same input always produces byte-identical output.
    """
    data = snapshot.model_dump(mode="json")
    data["disclaimer"] = EXPORT_DISCLAIMER
    return json.dumps(
        data,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# TXT mapper


def _fmt_date(d: date | datetime | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d %H:%M %Z").strip()
    return d.isoformat()


def _section_header(title: str) -> list[str]:
    bar = "=" * max(60, len(title) + 4)
    return [bar, f"  {title}", bar]


def _wrap_for_txt(
    text: str, *, indent: str = "", width: int = 72,
) -> list[str]:
    """Wrap a paragraph to ``width`` columns, prefixing each line
    with ``indent``. Used by the disclaimer block — keeps the TXT
    legible printed or pasted into an email."""
    words = text.split()
    out: list[str] = []
    current = indent
    for w in words:
        if len(current) + len(w) + 1 > width and current.strip():
            out.append(current.rstrip())
            current = indent + w
        else:
            current = (current + " " + w) if current.strip() else (current + w)
    if current.strip():
        out.append(current.rstrip())
    return out


def human_readable_txt_mapper(snapshot: ExportSnapshot) -> bytes:
    """Render a snapshot as a sectioned, human-readable text file.

    The skeleton emits a useful-but-minimal layout — enough to be
    legible printed or emailed. Mapper expansion (per-fact-type
    grouping, table-of-contents, evidence anchors, etc.) lands in
    M03 per the revised Group-C scope; Slice 4 just proves the
    plumbing.
    """
    lines: list[str] = []

    # Header
    lines.extend(_section_header("OwnChart Export"))
    lines.append(
        f"  Snapshot version : {snapshot.snapshot_version}"
    )
    lines.append(
        f"  Generated at     : {_fmt_date(snapshot.generated_at)}"
    )
    lines.append("")

    # Disclaimer — Slice 4 hardening (PM 2026-05-19, review #7).
    # Block-quoted between two divider lines so it's visually
    # distinct from the data sections that follow. Wrapped at ~72
    # columns for printability.
    lines.extend(_section_header("Patient packet — please read"))
    for chunk in _wrap_for_txt(EXPORT_DISCLAIMER, indent="  ", width=72):
        lines.append(chunk)
    lines.append("")

    # Record
    rec = snapshot.record
    lines.extend(_section_header(f"Record — {rec.display_name}"))
    lines.append(f"  Display name : {rec.display_name}")
    if rec.given_names or rec.family_name:
        lines.append(
            f"  Full name    : "
            f"{(rec.given_names or '').strip()} "
            f"{(rec.family_name or '').strip()}".rstrip()
        )
    if rec.birth_date:
        lines.append(f"  Birth date   : {_fmt_date(rec.birth_date)}")
    if rec.gender:
        lines.append(f"  Gender       : {rec.gender}")
    lines.append(f"  Self-record  : {'yes' if rec.is_self else 'no'}")
    lines.append("")

    # Sources
    lines.extend(_section_header(f"Sources ({len(snapshot.sources)})"))
    if snapshot.sources:
        for s in snapshot.sources:
            label = s.source_label or s.original_filename or "(unlabeled)"
            lines.append(
                f"  - [{s.source_type}] {label} "
                f"({_fmt_date(s.acquired_at or s.created_at)})"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Facts — grouped by year of date_start, then by fact_type
    lines.extend(_section_header(f"Facts ({len(snapshot.facts)})"))
    if snapshot.facts:
        by_year: dict[str, list] = defaultdict(list)
        for f in snapshot.facts:
            year = (
                f.date_start.strftime("%Y") if f.date_start else "undated"
            )
            by_year[year].append(f)
        for year in sorted(by_year.keys()):
            lines.append(f"  -- {year} --")
            for f in by_year[year]:
                date_part = _fmt_date(f.date_start) if f.date_start else "—"
                sig = f.significance or "-"
                lines.append(
                    f"    {date_part} [{f.fact_type}] {f.label} "
                    f"(sig={sig}, review={f.review_state})"
                )
    else:
        lines.append("  (none)")
    lines.append("")

    # Calendar sources
    lines.extend(_section_header(
        f"Calendar sources ({len(snapshot.calendar_sources)})"
    ))
    if snapshot.calendar_sources:
        for cs in snapshot.calendar_sources:
            state = "active" if cs.disconnected_at is None else "disconnected"
            lines.append(
                f"  - {cs.display_name} [{cs.adapter_type}] "
                f"privacy={cs.privacy_mode} "
                f"llm_full_details={cs.llm_full_details_consent} "
                f"({state})"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Calendar events
    lines.extend(_section_header(
        f"Calendar events ({len(snapshot.calendar_events)})"
    ))
    if snapshot.calendar_events:
        for ce in snapshot.calendar_events:
            title = ce.title or "(redacted)"
            all_day_marker = " [all-day]" if ce.all_day else ""
            lines.append(
                f"  {_fmt_date(ce.start_at)} → "
                f"{_fmt_date(ce.end_at)}{all_day_marker} {title} "
                f"(stored as: {ce.privacy_mode_applied})"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    return ("\n".join(lines) + "\n").encode("utf-8")
