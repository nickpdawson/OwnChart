"""Health Auto Export ingest — Apple Health / Watch metrics from the
Lybrary Health Auto Export iOS app (CSV / JSON / API push).

The iOS app emits a structured payload of HealthKit metrics:

    {
      "data": {
        "metrics": [
          {"name": "step_count", "units": "count",
           "data": [{"date": "2024-01-15 00:00:00 -0700", "qty": 8523}, ...]},
          {"name": "heart_rate", "units": "count/min",
           "data": [{"date": "...", "Min": 52, "Max": 188, "Avg": 78}, ...]},
          ...
        ],
        "workouts": [
          {"name": "Running", "start": "...", "end": "...",
           "duration": 0.75, "totalDistance": 5.2, "totalDistanceUnit": "km",
           "totalEnergyBurned": 412, "totalEnergyBurnedUnit": "kcal",
           ...}, ...
        ]
      }
    }

V1 scope per docs/03 Lane 4: sleep, workouts, steps/activity, HR,
resting HR, HRV, VO2 max, body metrics/weight, medications, symptoms.

Mapping strategy:
- One ExtractedFact per metric per day (vitals series get rolled up
  to daily so we don't drown in millions of HR samples).
- One ExtractedFact per workout session.
- One ExtractedFact per sleep session.
- One ExtractedFact per body-metric measurement (weight, BMI, etc.).
- One ExtractedFact per medication administration event.
- One ExtractedFact per symptom episode.
- All confidence=95 (sensor data is high-trust at the value level).
- Wearable / metric path: extraction_method='health_auto_export',
  review_state='confirmed' (passive sensor — auto-confirm; user can
  correct). Lands in the wearable lane on the global timeline.
- Self-reported clinical path (medications, symptoms):
  extraction_method='patient_self_report'. These are user-attested
  clinical events logged in the iPhone Health app, not passive sensor
  readings, so they belong in the clinical lane on the global
  timeline, not the wearable lane. Skipped doses use
  review_state='needs_review' (compliance signal worth surfacing).
- coded_concepts.hkquantitytype carries the HealthKit identifier so
  future linkage to clinical labs/observations stays possible.
- coded_concepts.rxnorm carries the RxNorm code(s) the iOS Health
  app attaches to recognized prescription medications.

Lab-shaped HealthKit quantities (e.g. HKQuantityTypeIdentifierBloodGlucose)
are deferred to the FHIR/CCDA lane per the locked-in V1 decision —
labs belong in the clinical lane, not the wearable lane.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..canonical.equivalence import daily_metric_key_from_auto_export

from ..core.logger import get_logger

log = get_logger("ownchart.ingest.auto_export")


# Metrics we ingest (V1 scope per docs/03 Lane 4 + Nick's locked-in
# decision in docs/05). Anything not in this set is logged + skipped
# so the same parser works against future expanded scopes without code
# churn — just add the metric here.
#
# Keys are the lowercase metric names the Health Auto Export app emits
# (see https://www.lybrary.app/healthautoexport/data-types). Values are
# (display label template, HealthKit identifier) tuples; the label
# template uses Python str.format() with `value` and `units`.
_DAILY_METRICS: dict[str, tuple[str, str]] = {
    "step_count":            ("Daily steps: {value:,.0f}",                "HKQuantityTypeIdentifierStepCount"),
    "active_energy":         ("Active energy: {value:,.0f} {units}",      "HKQuantityTypeIdentifierActiveEnergyBurned"),
    "basal_energy_burned":   ("Resting energy: {value:,.0f} {units}",     "HKQuantityTypeIdentifierBasalEnergyBurned"),
    "apple_exercise_time":   ("Exercise time: {value:.0f} {units}",       "HKQuantityTypeIdentifierAppleExerciseTime"),
    "apple_stand_hours":     ("Stand hours: {value:.0f}",                 "HKCategoryTypeIdentifierAppleStandHour"),
    "apple_stand_time":      ("Stand time: {value:.0f} {units}",          "HKQuantityTypeIdentifierAppleStandTime"),
    "flights_climbed":       ("Flights climbed: {value:.0f}",             "HKQuantityTypeIdentifierFlightsClimbed"),
    "walking_running_distance": ("Walking + running: {value:.2f} {units}", "HKQuantityTypeIdentifierDistanceWalkingRunning"),
    "heart_rate":            ("Heart rate avg/min/max",                   "HKQuantityTypeIdentifierHeartRate"),
    "resting_heart_rate":    ("Resting HR: {value:.0f} {units}",          "HKQuantityTypeIdentifierRestingHeartRate"),
    "heart_rate_variability": ("HRV (SDNN): {value:.0f} {units}",         "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"),
    "vo2_max":               ("VO₂ max: {value:.1f} {units}",             "HKQuantityTypeIdentifierVO2Max"),
    "respiratory_rate":      ("Respiratory rate: {value:.1f} {units}",    "HKQuantityTypeIdentifierRespiratoryRate"),
    "blood_oxygen_saturation": ("SpO₂: {value:.1%}",                      "HKQuantityTypeIdentifierOxygenSaturation"),
    "body_temperature":      ("Body temp: {value:.1f} {units}",           "HKQuantityTypeIdentifierBodyTemperature"),
    # Body metrics — these are typically infrequent (weekly+) so we
    # still write one fact per measurement. Treated as daily here for
    # simplicity; the data array usually has one entry per day max.
    "weight_body_mass":      ("Weight: {value:.1f} {units}",              "HKQuantityTypeIdentifierBodyMass"),
    "body_mass_index":       ("BMI: {value:.1f}",                         "HKQuantityTypeIdentifierBodyMassIndex"),
    "body_fat_percentage":   ("Body fat: {value:.1%}",                    "HKQuantityTypeIdentifierBodyFatPercentage"),
    "lean_body_mass":        ("Lean body mass: {value:.1f} {units}",      "HKQuantityTypeIdentifierLeanBodyMass"),
    "waist_circumference":   ("Waist: {value:.1f} {units}",               "HKQuantityTypeIdentifierWaistCircumference"),
}

# Sleep metric — sessions, not daily totals.
_SLEEP_METRIC = "sleep_analysis"


@dataclass
class AutoExportFact:
    fact_type: str  # 'observation' | 'workout' | 'medication' | 'symptom'
    label: str
    description: str | None
    date_start: datetime
    date_end: datetime | None
    coded_concepts: dict[str, list[str]]
    confidence: int = 95
    # Per-fact overrides for the worker's ExtractedFact creation. None
    # means "use the worker's default for the metric path"
    # (extraction_method='health_auto_export', review_state='confirmed',
    # anchor_type='auto_export_metric'). Medications and symptoms set
    # these explicitly so they land in the clinical lane on the global
    # timeline (not the wearable lane) and carry semantically-correct
    # anchor types.
    extraction_method: str | None = None
    review_state: str | None = None
    anchor_type: str | None = None
    # Source-neutral key — populated for daily-aggregate metrics so
    # the same day's steps/energy/etc. from native HK and Auto Export
    # collapse to one canonical event. None for facts without a clean
    # canonicalization rule (medications, workouts in V1, symptoms).
    equivalence_key: str | None = None
    # Idempotency key for re-pushes of the same logical sample.
    # Populated for medications since the Health Auto Export iOS app
    # ships the full medication history on every push instead of
    # deltas — without this, every push re-inserts the same scheduled
    # dose (16 "Celebrex / Taken" rows for one day, observed
    # 2026-05-15). Native-HK ingest uses the client-provided
    # client_sample_key from the device; here we derive a deterministic
    # one from (label, scheduled timestamp, status). Re-pushes hash
    # to the same key → ON CONFLICT DO NOTHING dedupes silently.
    client_sample_key: str | None = None
    # docs/07 Priority 1: reason copy for the Review Inbox. Only set
    # when the emitter has confident classification (e.g. Auto Export
    # medication with status=Skipped → "you logged this as Skipped").
    why_needs_review_code: str | None = None
    why_needs_review_text: str | None = None
    review_task_type: str | None = None
    source_context_only_eligible: bool = False


@dataclass
class AutoExportIngest:
    facts: list[AutoExportFact] = field(default_factory=list)
    metric_counts: dict[str, int] = field(default_factory=dict)
    workout_count: int = 0
    sleep_session_count: int = 0
    medication_count: int = 0
    symptom_count: int = 0
    skipped_metrics: list[str] = field(default_factory=list)
    # Top-level data sections we recognized as Auto Export shapes but
    # don't yet ingest (e.g. stateOfMind, cycleTracking, ecg). Logged
    # so future scope expansion is visible without code spelunking.
    unhandled_sections: list[str] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)

_TZ_PATTERN = re.compile(r"([+-])(\d{2}):?(\d{2})$")


def _parse_dt(s: Any) -> datetime | None:
    """Parse a Health Auto Export date string. The app uses
    'YYYY-MM-DD HH:MM:SS ±HHMM' but tolerates ISO 8601 too."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    # Normalize "+0000" / "+00:00" → strptime-friendly "+0000".
    s = _TZ_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}", s)
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
        except ValueError:
            continue
    # Last-ditch ISO parsing.
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def _emit_metric_fact(
    out: AutoExportIngest,
    metric_name: str,
    units: str,
    entry: dict[str, Any],
) -> None:
    """Map a single (metric, day) entry to one ExtractedFact."""
    label_tpl, hk_id = _DAILY_METRICS[metric_name]
    date_start = _parse_dt(entry.get("date"))
    if date_start is None:
        out.parse_warnings.append(f"{metric_name}: skipped entry with unparseable date")
        return
    # Heart rate is special — has Min/Max/Avg fields rather than `qty`.
    if metric_name == "heart_rate":
        avg = entry.get("Avg") or entry.get("avg")
        mn = entry.get("Min") or entry.get("min")
        mx = entry.get("Max") or entry.get("max")
        if avg is None and mn is None and mx is None:
            return
        bits = []
        if avg is not None:
            bits.append(f"avg {float(avg):.0f}")
        if mn is not None:
            bits.append(f"min {float(mn):.0f}")
        if mx is not None:
            bits.append(f"max {float(mx):.0f}")
        label = "Heart rate: " + ", ".join(bits) + f" {units}"
        description = None
    else:
        qty = entry.get("qty")
        if qty is None:
            qty = entry.get("Avg") or entry.get("avg")
        if qty is None:
            return
        try:
            value = float(qty)
        except (TypeError, ValueError):
            return
        try:
            label = label_tpl.format(value=value, units=units)
        except Exception:  # noqa: BLE001
            label = f"{metric_name}: {value} {units}"
        description = None

    out.facts.append(
        AutoExportFact(
            fact_type="observation",
            label=label[:512],
            description=description,
            date_start=date_start,
            date_end=None,
            coded_concepts={"hkquantitytype": [hk_id]},
            equivalence_key=daily_metric_key_from_auto_export(
                metric_name, date_start
            ),
        )
    )
    out.metric_counts[metric_name] = out.metric_counts.get(metric_name, 0) + 1


def _emit_workout(out: AutoExportIngest, w: dict[str, Any]) -> None:
    name = (w.get("name") or "Workout").strip() or "Workout"
    ds = _parse_dt(w.get("start"))
    de = _parse_dt(w.get("end"))
    if ds is None:
        out.parse_warnings.append(f"workout {name}: skipped (no start date)")
        return
    distance = w.get("totalDistance")
    distance_unit = w.get("totalDistanceUnit") or "km"
    energy = w.get("totalEnergyBurned")
    energy_unit = w.get("totalEnergyBurnedUnit") or "kcal"
    duration_h = w.get("duration")
    bits = []
    if isinstance(distance, (int, float)) and distance > 0:
        bits.append(f"{float(distance):.2f} {distance_unit}")
    if isinstance(duration_h, (int, float)) and duration_h > 0:
        mins = int(round(float(duration_h) * 60))
        bits.append(f"{mins} min")
    if isinstance(energy, (int, float)) and energy > 0:
        bits.append(f"{float(energy):.0f} {energy_unit}")
    label = name if not bits else f"{name}: " + ", ".join(bits)
    out.facts.append(
        AutoExportFact(
            fact_type="observation",
            label=label[:512],
            description=None,
            date_start=ds,
            date_end=de,
            coded_concepts={"hkworkoutactivity": [name]},
        )
    )
    out.workout_count += 1


def _emit_sleep(out: AutoExportIngest, s: dict[str, Any]) -> None:
    """One ExtractedFact per sleep session, with stage breakdown in the description."""
    ds = _parse_dt(s.get("startDate") or s.get("start"))
    de = _parse_dt(s.get("endDate") or s.get("end"))
    if ds is None:
        out.parse_warnings.append("sleep: skipped (no start date)")
        return
    # The Health Auto Export sleep payload keys vary slightly across
    # iOS versions. Cover the common spellings.
    asleep = (
        s.get("asleep")
        or s.get("totalSleep")
        or s.get("sleepDuration")
    )
    in_bed = s.get("inBed") or s.get("inBedDuration")
    deep = s.get("deep") or s.get("deepSleep")
    rem = s.get("rem") or s.get("remSleep")
    awake = s.get("awake") or s.get("awakeDuration")

    def _hm(hours: Any) -> str | None:
        if not isinstance(hours, (int, float)):
            return None
        m = int(round(float(hours) * 60))
        h, m2 = divmod(m, 60)
        return f"{h}h {m2}m" if h else f"{m2}m"

    asleep_s = _hm(asleep)
    label = f"Sleep: {asleep_s}" if asleep_s else "Sleep session"
    parts = []
    for name, val in (("in bed", in_bed), ("deep", deep), ("REM", rem), ("awake", awake)):
        v = _hm(val)
        if v:
            parts.append(f"{name} {v}")
    description = "; ".join(parts) if parts else None

    out.facts.append(
        AutoExportFact(
            fact_type="observation",
            label=label[:512],
            description=description,
            date_start=ds,
            date_end=de,
            coded_concepts={"hkquantitytype": ["HKCategoryTypeIdentifierSleepAnalysis"]},
        )
    )
    out.sleep_session_count += 1


# RxNorm system URL the iOS Health app emits in medication codings[].
# We match case-insensitively on substring rather than exact URL so
# ".../umls/rxnorm" and bare "rxnorm" both work.
_RXNORM_MARKER = "rxnorm"


def _emit_medication(out: AutoExportIngest, m: dict[str, Any]) -> None:
    """One ExtractedFact per medication administration event.

    The Auto Export iOS app emits one entry per scheduled dose with
    `status` (Taken | Skipped | Not Interacted | ...). Each entry is
    its own administration record — the dossier cluster route will
    collapse repeated administrations of the same drug under one card
    via the `displayText`-derived label.

    extraction_method='patient_self_report' is intentional: medications
    are user-attested clinical events (the user logged them in the
    iPhone Health app), not passive sensor readings. That keeps them
    in the clinical lane on the global timeline rather than the
    wearable lane alongside heart-rate density.

    Verified shape from a real KP-side payload (May 2026): keys
    {displayText, codings[{system, code, version}], dosage,
     scheduledDosage, units, start, end, scheduledDate, status,
     isArchived, nickname?, form?}. `codings[].system` for RxNorm is
     "http://www.nlm.nih.gov/research/umls/rxnorm".
    """
    name = (m.get("displayText") or "").strip()
    if not name:
        out.parse_warnings.append("medication: skipped (no displayText)")
        return

    ds = _parse_dt(m.get("start") or m.get("scheduledDate"))
    if ds is None:
        out.parse_warnings.append(f"medication {name}: skipped (no start date)")
        return
    de = _parse_dt(m.get("end")) or ds

    parts: list[str] = []
    status = (m.get("status") or "").strip()
    if status:
        parts.append(status)
    dosage = m.get("dosage")
    units = (m.get("units") or "").strip()
    if isinstance(dosage, (int, float)) and dosage and dosage != 1:
        parts.append(f"{dosage:g} {units}".strip() if units else f"dose {dosage:g}")
    nickname = (m.get("nickname") or "").strip()
    if nickname and nickname.lower() not in name.lower():
        parts.append(f"({nickname})")
    description = " · ".join(parts) if parts else None

    rxnorm_codes: list[str] = []
    for c in m.get("codings") or []:
        if not isinstance(c, dict):
            continue
        sys_url = (c.get("system") or "").strip().lower()
        code = (c.get("code") or "").strip()
        if code and _RXNORM_MARKER in sys_url:
            rxnorm_codes.append(code)
    coded: dict[str, list[str]] = {}
    if rxnorm_codes:
        coded["rxnorm"] = rxnorm_codes

    # Skipped doses are still useful evidence (compliance signal) but
    # less authoritative than confirmed administrations — flag them
    # for review. Taken / Not Interacted = confirmed.
    review = "needs_review" if status == "Skipped" else "confirmed"

    # docs/07 Priority 1: reason for the Review Inbox. Only Skipped
    # doses land in review; "you logged this as Skipped" is exactly
    # what the user needs to confirm or correct.
    why_code = "auto_export_dose_skipped" if review == "needs_review" else None
    why_text = (
        f"You logged this dose of {name} as Skipped on "
        f"{ds.date().isoformat()}."
        if review == "needs_review"
        else None
    )

    # Deterministic dedup key. The Auto Export iOS app re-pushes the
    # full medication history on every push instead of deltas. Without
    # this key the same scheduled dose lands as N copies in
    # extracted_facts (observed: 16 identical Celebrex / Taken rows
    # for 2026-05-13). The key includes drug + exact scheduled time
    # (second precision) + adherence status, so:
    #   - Re-pushes of the same scheduled dose collapse on the partial
    #     unique index over client_sample_key (ON CONFLICT DO NOTHING).
    #   - Two real distinct doses on the same day (morning Taken,
    #     evening Skipped) keep separate keys.
    blob = (
        f"auto-export:medication:"
        f"{name.lower().strip()}:"
        f"{ds.replace(microsecond=0).isoformat()}:"
        f"{(status or '').lower()}"
    )
    csk = "ae-med-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    out.facts.append(
        AutoExportFact(
            fact_type="medication",
            label=name[:512],
            description=description,
            date_start=ds,
            date_end=de,
            coded_concepts=coded,
            extraction_method="patient_self_report",
            review_state=review,
            anchor_type="auto_export_medication",
            why_needs_review_code=why_code,
            why_needs_review_text=why_text,
            review_task_type="medication_dose_log" if review == "needs_review" else None,
            client_sample_key=csk,
        )
    )
    out.medication_count += 1


def _emit_symptom(out: AutoExportIngest, s: dict[str, Any]) -> None:
    """One ExtractedFact per symptom episode.

    Inferred shape (from Auto Export wiki — not yet verified against a
    real Nick payload): keys {start, end, name, severity, userEntered,
    source}. severity ∈ {Not Present, Mild, Moderate, Severe,
    Unspecified}. Defensive: each field is optional; we skip entries
    without a name + start.
    """
    name = (s.get("name") or "").strip()
    if not name:
        out.parse_warnings.append("symptom: skipped (no name)")
        return
    ds = _parse_dt(s.get("start") or s.get("startDate"))
    if ds is None:
        out.parse_warnings.append(f"symptom {name}: skipped (no start date)")
        return
    de = _parse_dt(s.get("end") or s.get("endDate")) or ds

    severity = (s.get("severity") or "").strip()
    description = severity if severity and severity != "Unspecified" else None

    out.facts.append(
        AutoExportFact(
            fact_type="symptom",
            label=name[:512],
            description=description,
            date_start=ds,
            date_end=de,
            coded_concepts={},
            extraction_method="patient_self_report",
            review_state="confirmed",
            anchor_type="auto_export_symptom",
        )
    )
    out.symptom_count += 1


# Top-level Auto Export data sections we recognize but don't yet
# ingest. Listed so the parser logs them by name (vs them disappearing
# silently the way an unknown metric currently does). Per the Auto
# Export wiki: stateOfMind, cycleTracking, ecg, heartRateNotifications.
_RECOGNIZED_UNHANDLED_SECTIONS = {
    "stateofmind",
    "cycletracking",
    "ecg",
    "heartratenotifications",
}


def parse_health_auto_export(payload: Any) -> AutoExportIngest:
    """Parse the JSON payload from the Health Auto Export iOS app.

    Tolerant of partial / malformed inputs — skips bad entries with
    a parse_warning rather than failing the whole upload.
    """
    out = AutoExportIngest()
    if not isinstance(payload, dict):
        out.parse_warnings.append("payload is not a JSON object")
        return out
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    # Metrics
    for m in data.get("metrics", []) or []:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip().lower()
        units = (m.get("units") or "").strip()
        entries = m.get("data") or []
        if name == _SLEEP_METRIC:
            for s in entries:
                if isinstance(s, dict):
                    _emit_sleep(out, s)
            continue
        if name not in _DAILY_METRICS:
            out.skipped_metrics.append(name or "(unnamed)")
            continue
        for e in entries:
            if isinstance(e, dict):
                _emit_metric_fact(out, name, units, e)

    # Workouts
    for w in data.get("workouts", []) or []:
        if isinstance(w, dict):
            _emit_workout(out, w)

    # Medications (verified shape)
    for med in data.get("medications", []) or []:
        if isinstance(med, dict):
            _emit_medication(out, med)

    # Symptoms (inferred shape — defensive)
    for sym in data.get("symptoms", []) or []:
        if isinstance(sym, dict):
            _emit_symptom(out, sym)

    # Surface any other top-level Auto Export sections present in the
    # payload so future scope expansion is visible. We don't ingest
    # them yet; they get logged in raw_metadata.unhandled_sections.
    for key in data.keys():
        if key in {"metrics", "workouts", "medications", "symptoms"}:
            continue
        if key.lower() in _RECOGNIZED_UNHANDLED_SECTIONS:
            section = data.get(key)
            count = len(section) if isinstance(section, list) else 0
            out.unhandled_sections.append(f"{key}({count})")
        else:
            out.unhandled_sections.append(f"{key}(?)")

    return out


def parse_health_auto_export_csv(metric_name: str, rows: Iterable[dict[str, Any]]) -> AutoExportIngest:
    """Parse one CSV file from Health Auto Export.

    Each app-emitted CSV holds a single metric, with `date` + `qty`
    columns (plus Min/Max/Avg for HR). Caller passes the metric name
    (typically the filename minus extension) and the parsed dict rows.
    """
    out = AutoExportIngest()
    name = (metric_name or "").strip().lower()
    if name not in _DAILY_METRICS and name != _SLEEP_METRIC:
        out.skipped_metrics.append(name or "(unknown)")
        return out
    units = ""
    if name == _SLEEP_METRIC:
        for r in rows:
            _emit_sleep(out, r)
    else:
        for r in rows:
            # CSV doesn't carry units in-band; the unit is implicit per
            # metric type. Best-effort: infer for the small set we know.
            _emit_metric_fact(out, name, units, r)
    return out
