"""Slice 4 export mappers — OwnChart JSON + TXT + Pictal Health Record v1.0.

All mappers take a fully-built ``ExportSnapshot`` (pure data; no
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

  pictal_health_json_mapper(snapshot) → JSON bytes
    Deterministic ``Pictal Health Record v1.0`` shape — buckets
    facts into the nine Pictal sections, preserves date precision
    (YYYY / YYYY-MM / YYYY-MM-DD / null), classifies active vs
    resolved, and silently excludes high-volume body-signal rows
    (HealthKit / auto-export). This is a *download* the user
    imports into Pictal; OwnChart does not contact Pictal.

All three are pure: same input, same output, no I/O.
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


# ---------------------------------------------------------------------------
# Pictal Health Record v1.0 mapper
#
# Deterministic JSON in the shape Pictal Health publishes for their
# v1.0 import format. The user downloads this file from OwnChart and
# imports it manually into Pictal — OwnChart does NOT contact Pictal,
# there is no API integration, and Pictal does not see anything the
# user did not choose to download.
#
# Design constraints (from PM, 2026-06-10):
#   - Pure function, no LLM.
#   - Read-only over ExportSnapshot. No DB. No I/O.
#   - Stable: same snapshot → byte-identical bytes (sort_keys=True).
#   - Body-signal facts (HealthKit / auto-export raw measurements)
#     are silently excluded — Pictal isn't shaped for daily HR/steps
#     and dumping them would dilute the clinical record.
#   - Date precision is preserved on the wire: year → "YYYY",
#     month → "YYYY-MM", day-or-finer → "YYYY-MM-DD", unknown → null.
#   - Provenance lives in `notes` (Pictal has no native provenance
#     field). Never include internal UUIDs.
#   - Source semantics, not invention: when historical_status /
#     date_provenance don't actually tell us the fact is resolved,
#     we leave status as null rather than guess.

_PICTAL_FORMAT_VERSION = "Pictal Health Record v1.0"

# Body-signal extraction methods. The Pictal mapper drops these
# facts entirely — Pictal isn't a quantified-self dump target. The
# domain filter in the snapshot builder may already exclude them at
# query time when the user deselects "body signals," but the mapper
# is the second wall: even if a future caller includes body-signal
# facts in the snapshot, Pictal JSON will not carry them.
_BODY_SIGNAL_EXTRACTION_METHODS: frozenset[str] = frozenset({
    "health_auto_export",
    "native_healthkit",
})

# fact_type → Pictal section. Lower-case match. Anything that doesn't
# map here (e.g. raw observation rows without a clinically-meaningful
# label) gets dropped. The route from raw fact_type to Pictal section
# is deliberately conservative: we'd rather omit ambiguous rows than
# dump them into the wrong bucket.
_PICTAL_SECTION_FOR_FACT_TYPE: dict[str, str] = {
    # Diagnoses / problems
    "condition":                "diagnoses",
    "diagnosis":                "diagnoses",
    "problem":                  "diagnoses",
    "allergy":                  "diagnoses",
    "allergy_intolerance":      "diagnoses",
    # Medications + treatments
    "medication":               "medications_and_treatments",
    "medication_request":       "medications_and_treatments",
    "medication_statement":     "medications_and_treatments",
    "treatment":                "medications_and_treatments",
    "therapy":                  "medications_and_treatments",
    # Surgeries + procedures (immunizations file here too — Pictal
    # treats them as procedures rather than carving a vaccines bucket)
    "procedure":                "surgeries_and_procedures",
    "surgery":                  "surgeries_and_procedures",
    "operation":                "surgeries_and_procedures",
    "immunization":             "surgeries_and_procedures",
    "vaccination":              "surgeries_and_procedures",
    # Hospitalizations
    "hospitalization":          "hospitalizations",
    "admission":                "hospitalizations",
    "encounter_inpatient":      "hospitalizations",
    "inpatient_encounter":      "hospitalizations",
    # Tests + imaging
    "lab":                      "tests_and_imaging",
    "lab_result":               "tests_and_imaging",
    "imaging":                  "tests_and_imaging",
    "imaging_study":            "tests_and_imaging",
    "test":                     "tests_and_imaging",
    "diagnostic_report":        "tests_and_imaging",
    "observation":              "tests_and_imaging",
    # Injuries + illnesses + acute events
    "injury":                   "injuries_and_illnesses",
    "illness":                  "injuries_and_illnesses",
    "infection":                "injuries_and_illnesses",
    "acute_event":              "injuries_and_illnesses",
    # Symptoms
    "symptom":                  "symptoms",
    # Substance use
    "substance":                "substance_use",
    "substance_use":            "substance_use",
    "tobacco":                  "substance_use",
    "alcohol":                  "substance_use",
    "cannabis":                 "substance_use",
    # Explicit life events
    "life_event":               "life_events",
}

# Section keys in Pictal v1.0, fixed order so the JSON renders the
# same way every time. Sections with no facts still appear, as empty
# arrays — that's the documented v1.0 shape.
_PICTAL_SECTIONS: tuple[str, ...] = (
    "diagnoses",
    "medications_and_treatments",
    "surgeries_and_procedures",
    "hospitalizations",
    "tests_and_imaging",
    "injuries_and_illnesses",
    "symptoms",
    "substance_use",
    "life_events",
)

# historical_status values that imply the condition is no longer active.
_RESOLVED_HISTORICAL_STATUSES: frozenset[str] = frozenset({
    "resolved",
    "inactive",
    "remission",
    "history_of",  # Section C: "history of X" means past, not present.
})

# fact_types where "no resolved signal" → default "active". Conditions,
# medications, and symptoms have a meaningful default; for tests /
# procedures / hospitalizations the concept of active/resolved doesn't
# apply, so we leave status as null.
_DEFAULTS_TO_ACTIVE: frozenset[str] = frozenset({
    "diagnoses",
    "medications_and_treatments",
    "symptoms",
    "substance_use",
})


def _pictal_date(
    dt: date | datetime | None, precision: str | None,
) -> str | None:
    """Render a fact date honoring its stored precision.

    Pictal v1.0 accepts a free-form date string per item; preserving
    precision avoids fabricating day-of-month accuracy we don't have.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        d = dt.date()
    else:
        d = dt
    p = (precision or "").lower()
    if p in ("year", "y"):
        return d.strftime("%Y")
    if p in ("month", "ym", "year_month"):
        return d.strftime("%Y-%m")
    # day / full / unspecified → full date. (Stored DB precision goes
    # finer than "day" for some methods, but Pictal v1.0 is day-grain.)
    return d.strftime("%Y-%m-%d")


def _pictal_status(fact, pictal_section: str) -> str | None:
    """Classify a fact as 'active' / 'resolved' / None.

    Resolution signals, in order:
      1. historical_status ∈ {resolved, inactive, remission, history_of}
         → resolved.
      2. date_end is set → resolved (the event ended).
      3. fact_type's pictal_section defaults to active (diagnoses,
         medications, symptoms, substance_use) AND no resolved signal
         → active. Source semantics support this default — these are
         the only buckets where ongoing-by-default is honest.
      4. Anything else → None. We don't claim a status we can't honestly
         derive (tests, procedures, hospitalizations don't have an
         active/resolved concept).
    """
    hist = (fact.historical_status or "").lower()
    if hist in _RESOLVED_HISTORICAL_STATUSES:
        return "resolved"
    if fact.date_end is not None:
        return "resolved"
    if pictal_section in _DEFAULTS_TO_ACTIVE:
        return "active"
    return None


def _pictal_notes_for(fact) -> str | None:
    """Compact human-readable provenance + description for `notes`.

    Pictal has no native provenance field, so the few honest signals
    we have (description, date_provenance hint) get folded into a
    single short string. Never includes internal UUIDs or source
    document IDs. Returns None when nothing useful exists.

    Format examples:
      - "Spasms in shoulder when reaching overhead."  (description only)
      - "Patient-confirmed date."                     (user_canonical)
      - "Source-documented date."                     (this_visit)
      - "Approximate date."                           (approximate)
      - "Patient-confirmed date. Spasms in shoulder…" (combined)
    """
    pieces: list[str] = []
    prov = (fact.date_provenance or "").lower()
    if prov == "this_visit":
        pieces.append("Source-documented date.")
    elif prov == "approximate":
        pieces.append("Approximate date.")
    elif prov in ("user_confirmed", "user_canonical"):
        pieces.append("Patient-confirmed date.")
    # description is the fact's longer text — copy as-is, no LLM
    # rewriting. Trim trailing whitespace but otherwise preserve.
    if fact.description and fact.description.strip():
        pieces.append(fact.description.strip())
    if not pieces:
        return None
    return " ".join(pieces)


def _pictal_patient(record) -> dict:
    """Build the top-level patient block.

    `name`: prefer "given family" if both names present (or one of
    them); otherwise display_name. We do not invent.
    `date_of_birth`: birth_date verbatim if present, else null.
    `notes`: omitted — no honest source.
    """
    given = (record.given_names or "").strip()
    family = (record.family_name or "").strip()
    if given or family:
        name = f"{given} {family}".strip()
    else:
        name = record.display_name
    out: dict = {
        "name": name,
        "date_of_birth": (
            record.birth_date.strftime("%Y-%m-%d")
            if record.birth_date is not None
            else None
        ),
    }
    return out


def _pictal_item(fact, pictal_section: str) -> dict:
    """Map one fact → one Pictal item dict. Item keys are stable
    across all sections so consumers don't need section-specific
    parsing."""
    return {
        "label": fact.label,
        "date": _pictal_date(fact.date_start, fact.date_precision),
        "date_end": _pictal_date(fact.date_end, fact.date_precision),
        "status": _pictal_status(fact, pictal_section),
        "notes": _pictal_notes_for(fact),
    }


def _fact_sort_key(fact) -> tuple:
    """Stable ordering inside a Pictal section.

    Primary: date_start ascending, NULLs last (consistent with the
    OwnChart UI's chronological default).
    Secondary: label (case-insensitive) so two same-day items render
    deterministically.
    """
    has_date = fact.date_start is not None
    if has_date:
        d = fact.date_start
        if isinstance(d, datetime):
            d = d.date()
        return (0, d.isoformat(), (fact.label or "").lower())
    return (1, "", (fact.label or "").lower())


def pictal_health_json_mapper(snapshot: ExportSnapshot) -> bytes:
    """Render the snapshot as Pictal Health Record v1.0 JSON.

    See module docstring for the design contract. Pure: same snapshot
    in → byte-identical JSON out.
    """
    sections: dict[str, list[dict]] = {k: [] for k in _PICTAL_SECTIONS}

    for f in snapshot.facts:
        # Body-signal facts: skipped regardless of fact_type. This is
        # the second wall behind the snapshot's domain filter.
        if (f.extraction_method or "") in _BODY_SIGNAL_EXTRACTION_METHODS:
            continue
        # Rejected facts: out. The user said "no, this isn't true."
        if (f.review_state or "") == "rejected":
            continue
        section = _PICTAL_SECTION_FOR_FACT_TYPE.get((f.fact_type or "").lower())
        if section is None:
            # No safe bucket → omit. Better than guessing.
            continue
        sections[section].append((f, section))

    # Sort each section deterministically, then convert to item dicts.
    out_sections: dict[str, list[dict]] = {}
    for key in _PICTAL_SECTIONS:
        items = sections[key]
        items.sort(key=lambda pair: _fact_sort_key(pair[0]))
        out_sections[key] = [_pictal_item(f, sec) for (f, sec) in items]

    payload = {
        "_format": _PICTAL_FORMAT_VERSION,
        "patient": _pictal_patient(snapshot.record),
        **out_sections,
    }
    return json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
