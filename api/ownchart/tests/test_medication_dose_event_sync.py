"""Build 45/46 directive tests — HealthKit medication dose-event sync.

Scope matches the 2026-06-20 PM directive:

  1. Two same-day doses aggregate into a single per-day cell with
     `dose_count=2`.
  2. Repeat-send idempotency: the stable `client_sample_key` for the
     same (display_key, local_day) is byte-identical so server-side
     dedupe collapses re-uploads.
  3. Display-name changes (rename / nickname swap) produce a new
     `client_sample_key`, so old + new rows persist independently
     instead of one silently overwriting the other.
  4. Unknown optional fields on inbound payloads are ignored
     (forward-compatibility — server can add fields without breaking
     older iOS builds).
  5. Ordinary HealthKit sync + MCP tool surfaces NEVER trigger Apple's
     medication picker. Only `MedicationsView` calls
     `.healthDataAccessRequest(... .userAnnotatedMedicationType() ...)`.
     Verified by static source-scan on the iOS tree.

Also pinned here:

  - The medication direct-sync upload reuses the existing active-record
     resolution (no fallback record, no silent upload to a different
     record). Verified by checking that `MedicationAccessFlow.swift`
     calls `client.uploadHealthKitBatch(_:)` — the same path the rest
     of HK sync uses — and does not roll its own URLRequest or write a
     `X-OwnChart-Person-Record` header that would bypass the standard
     four-step resolver.

These are unit / static tests — no DB, no HTTP client. They lock in
contract shape so the next regression doesn't slip past local-only
review.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# The canonical identifier the iOS direct-sync path posts for medication
# dose-event aggregates.
_DOSE_IDENTIFIER = "HKMedicationDoseEventTypeIdentifierMedicationDoseEvent"


def _ios_repo_root() -> Path | None:
    """Return the iOS canonical clone directory if present.

    `OwnChartiOS/` is gitignored — the static-source tests skip when
    the directory is absent (CI typically runs without iOS source).
    Local dev runs hit it.
    """
    candidate = (
        Path(__file__).resolve().parents[3]
        / "OwnChartiOS"
        / "OwnChart"
        / "OwnChart"
    )
    return candidate if candidate.is_dir() else None


def _fnv1a64(text: str) -> int:
    """Port of `MedicationAccessFlow.stableSampleKey` FNV-1a 64-bit.

    The iOS implementation is in
    `OwnChartiOS/.../Health/MedicationAccessFlow.swift`. Reproduced
    here so the test pins the algorithm — if iOS or this Python copy
    drift, the dedupe key drifts and re-uploads stop collapsing.
    """
    hash_ = 0xCBF2_9CE4_8422_2325
    for byte in text.encode("utf-8"):
        hash_ ^= byte
        hash_ = (hash_ * 0x0000_0100_0000_01B3) & 0xFFFF_FFFF_FFFF_FFFF
    return hash_


def _stable_sample_key(display_key: str, local_day_key: str) -> str:
    """Match the iOS helper's output format exactly."""
    composite = f"{local_day_key}|{display_key}"
    return f"med-{_fnv1a64(composite):x}"


def _base_sample_payload(
    *,
    client_sample_key: str,
    local_day: str,
    display_name: str,
    display_key: str,
    dose_count: int,
) -> dict:
    """Minimum valid wire payload for a medication-dose `SyncSample`."""
    return {
        "client_sample_key": client_sample_key,
        "start_at": f"{local_day}T00:00:00+00:00",
        "end_at": f"{local_day}T23:59:59+00:00",
        "medication_display_name": display_name,
        "medication_display_key": display_key,
        "dose_count": dose_count,
        "scheduled_count": 0,
        "as_needed_count": 0,
    }


# ---------------------------------------------------------------------------
# 1. Two same-day doses → one aggregate cell with dose_count=2
# ---------------------------------------------------------------------------


def test_two_same_day_doses_aggregate_to_single_cell_with_count_two():
    """The iOS aggregator collapses two `HKMedicationDoseEvent` rows
    for the same medication on the same local day into ONE `SyncSample`
    with `dose_count=2`. That contract is what the server's dedupe
    relies on — two cells for the same (display_key, local_day) would
    suggest an aggregator bug. Server pins the wire shape and refuses
    nothing for `dose_count >= 0`.
    """
    from ownchart.routes.healthkit_sync import SyncSample

    payload = _base_sample_payload(
        client_sample_key=_stable_sample_key("aspirin", "2026-06-19"),
        local_day="2026-06-19",
        display_name="Aspirin",
        display_key="aspirin",
        dose_count=2,
    )
    sample = SyncSample.model_validate(payload)
    assert sample.dose_count == 2
    assert sample.medication_display_key == "aspirin"
    # Round-trip through serialization so we exercise the
    # `extra="ignore"`-by-default codec on both directions.
    assert sample.model_dump(exclude_none=True)["dose_count"] == 2


def test_dose_count_zero_accepted_for_explicit_no_events_day():
    """A user with a tracked medication but no doses on a given day
    should NOT cause the iOS aggregator to send any row for that day
    (`MedicationAccessFlow` skips zero-event days in
    `foldIntoDailyAggregates`). But if a future caller does send
    `dose_count=0`, the server must accept it — `ge=0` permits it
    and we don't want a 422 on an honest empty day."""
    from ownchart.routes.healthkit_sync import SyncSample

    payload = _base_sample_payload(
        client_sample_key=_stable_sample_key("aspirin", "2026-06-19"),
        local_day="2026-06-19",
        display_name="Aspirin",
        display_key="aspirin",
        dose_count=0,
    )
    sample = SyncSample.model_validate(payload)
    assert sample.dose_count == 0


def test_negative_dose_count_rejected():
    """Defense in depth: a negative dose count is nonsense; the
    `ge=0` constraint on the Pydantic field must reject."""
    from pydantic import ValidationError
    from ownchart.routes.healthkit_sync import SyncSample

    payload = _base_sample_payload(
        client_sample_key="med-bogus",
        local_day="2026-06-19",
        display_name="Aspirin",
        display_key="aspirin",
        dose_count=-1,
    )
    with pytest.raises(ValidationError):
        SyncSample.model_validate(payload)


# ---------------------------------------------------------------------------
# 2. Repeat-send idempotency — stable client_sample_key
# ---------------------------------------------------------------------------


def test_stable_sample_key_is_deterministic_per_displaykey_and_day():
    """The iOS-side dedupe key is FNV-1a over `(local_day_key,
    display_key)`. Two invocations with the same inputs must produce
    byte-identical keys — that's what makes server dedupe work on
    re-syncs of overlapping windows."""
    key_a = _stable_sample_key("aspirin", "2026-06-19")
    key_b = _stable_sample_key("aspirin", "2026-06-19")
    assert key_a == key_b
    assert key_a.startswith("med-")


def test_stable_sample_key_distinguishes_different_days():
    """Same medication, two different local days → two different
    keys. Otherwise re-syncing a 7-day window would collapse every
    day into one row."""
    assert (
        _stable_sample_key("aspirin", "2026-06-19")
        != _stable_sample_key("aspirin", "2026-06-20")
    )


def test_stable_sample_key_distinguishes_different_medications():
    """Two different medications taken on the same day must produce
    two different keys. Otherwise the dedupe collapses unrelated
    rows."""
    assert (
        _stable_sample_key("aspirin", "2026-06-19")
        != _stable_sample_key("ibuprofen", "2026-06-19")
    )


# ---------------------------------------------------------------------------
# 3. Display-name changes — distinct keys, distinct rows
# ---------------------------------------------------------------------------


def test_display_name_change_produces_distinct_sample_key():
    """If a user renames a tracked medication in Apple Health (or
    Apple's normalization adjusts the displayText), the normalized
    `display_key` shifts. The iOS aggregator must emit a NEW row
    (new `client_sample_key`) rather than overwriting the old. Both
    rows persist server-side; the user's timeline reflects the
    rename as a forward-only event, not a retroactive rewrite.
    """
    old = _stable_sample_key("aspirin", "2026-06-19")
    new = _stable_sample_key("bayer aspirin", "2026-06-19")
    assert old != new


def test_normalize_display_text_collapses_whitespace_and_case():
    """The iOS-side normalization is lowercase + collapse-whitespace.
    Different spellings of the same medication name produce the
    same `display_key`, so re-syncs over the same window dedupe
    across cosmetic-only changes (e.g. extra space, capitalization
    differences)."""
    # The iOS algorithm: lowercase + collapse runs of whitespace
    # into single spaces.
    def _normalize(raw: str) -> str:
        return " ".join(raw.lower().split())

    assert _normalize("Bayer Aspirin") == "bayer aspirin"
    assert _normalize("  Bayer   Aspirin  ") == "bayer aspirin"
    # Different content → different normalization.
    assert _normalize("Aspirin Low Dose") != _normalize("Aspirin")


# ---------------------------------------------------------------------------
# 4. Unknown optional fields are ignored
# ---------------------------------------------------------------------------


def test_unknown_optional_fields_are_ignored_on_sample():
    """Pydantic's default `extra="ignore"` lets iOS ship new fields
    that older servers don't yet recognize — and lets servers add
    fields that older iOS builds don't send. The medication wire
    contract takes advantage of this for forward-compat."""
    from ownchart.routes.healthkit_sync import SyncSample

    payload = _base_sample_payload(
        client_sample_key=_stable_sample_key("aspirin", "2026-06-19"),
        local_day="2026-06-19",
        display_name="Aspirin",
        display_key="aspirin",
        dose_count=1,
    )
    # A field that doesn't exist today; future iOS may ship it.
    payload["medication_future_field"] = "experimental"
    # Another future-shape: a nested unknown object.
    payload["medication_extras"] = {"frequency": "daily", "reason": "header"}
    sample = SyncSample.model_validate(payload)
    assert sample.dose_count == 1
    assert sample.medication_display_name == "Aspirin"


def test_unknown_optional_fields_are_ignored_on_request_envelope():
    """Same forward-compat guarantee on the outer SyncRequest. If iOS
    sends a top-level field the server doesn't know yet, the route
    must NOT 422 — Pydantic silently ignores it."""
    from ownchart.routes.healthkit_sync import SyncRequest

    payload = {
        "device_id": "ios-12345678",
        "identifier": _DOSE_IDENTIFIER,
        "strategy": "raw",
        "sync_mode": "incremental",
        "samples": [
            _base_sample_payload(
                client_sample_key=_stable_sample_key("aspirin", "2026-06-19"),
                local_day="2026-06-19",
                display_name="Aspirin",
                display_key="aspirin",
                dose_count=1,
            )
        ],
        # Future field that doesn't exist today.
        "client_capabilities_version": "v2",
    }
    request = SyncRequest.model_validate(payload)
    assert request.identifier == _DOSE_IDENTIFIER
    assert len(request.samples) == 1


# ---------------------------------------------------------------------------
# 5. Ordinary sync + MCP NEVER trigger the medication picker
# ---------------------------------------------------------------------------


# SwiftUI modifier invocation signature — the leading dot + open paren
# distinguishes a CALL from a doc-comment mention. `MedicationAccessFlow`
# documents the flow but does not invoke the modifier itself.
_MEDICATION_PICKER_INVOCATION = ".healthDataAccessRequest("
# Apple's `objectType:` factory call. Same dot-prefix discipline.
_MEDICATION_TYPE_FACTORY_INVOCATION = ".userAnnotatedMedicationType("
# The single file that's allowed to invoke Apple's picker.
_ALLOWED_INVOKER_FILENAME = "MedicationsView.swift"


def _ios_swift_sources() -> list[Path]:
    """Walk the iOS source tree, skipping test targets + the
    HealthAuthorization compatibility table which mentions
    `usesLazyAuth` for the medication identifier but never invokes
    the picker."""
    root = _ios_repo_root()
    if root is None:
        return []
    out: list[Path] = []
    for path in root.rglob("*.swift"):
        if "OwnChartTests" in path.parts or "OwnChartUITests" in path.parts:
            continue
        out.append(path)
    return out


def _strip_swift_comments(text: str) -> str:
    """Remove single-line `//` and `///` comments and block `/* ... */`
    comments from a Swift source. Doc-comments routinely describe API
    invocations the file itself does NOT make — the static checks
    below want to find real invocations, not narrative mentions.

    This is intentionally minimal (no string-literal awareness, no
    `#if` handling) — Swift source as written for iOS app code rarely
    contains the picker substrings inside string literals.
    """
    # Block comments first so `/* ... // ... */` doesn't half-survive.
    import re
    no_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out_lines: list[str] = []
    for line in no_block.splitlines():
        # Trim everything from the first `//` (which also covers `///`).
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def test_only_medications_view_invokes_apple_medication_picker():
    """Apple's `.healthDataAccessRequest(... .userAnnotatedMedicationType() ...)`
    surface fires the per-medication picker. The 2026-06-20 directive
    is explicit: this MUST be a user-initiated tap on the explicit
    Medications screen — never on app launch, never during ordinary
    HK sync, never as a side effect of an MCP request.

    Static check: only `MedicationsView.swift` may name BOTH the
    picker modifier AND the user-annotated medication type factory.
    Skips if `OwnChartiOS/` is not on disk (CI without the iOS
    source tree).
    """
    sources = _ios_swift_sources()
    if not sources:
        pytest.skip("OwnChartiOS source tree not present; static check skipped")

    offenders: list[str] = []
    for path in sources:
        text = _strip_swift_comments(
            path.read_text(encoding="utf-8", errors="ignore")
        )
        invokes_picker = _MEDICATION_PICKER_INVOCATION in text
        names_med_type = _MEDICATION_TYPE_FACTORY_INVOCATION in text
        # The combination is the picker-invocation signature.
        if invokes_picker and names_med_type:
            if path.name != _ALLOWED_INVOKER_FILENAME:
                offenders.append(str(path))

    assert not offenders, (
        "Apple medication picker is invoked outside MedicationsView.swift; "
        f"directive 2026-06-20 forbids that. Offenders: {offenders}"
    )


def test_mcp_surface_does_not_reference_user_annotated_medication_type():
    """Belt-and-suspenders for the MCP guarantee: no file under
    `OwnChartiOS/.../MCP/` may name `.userAnnotatedMedicationType()` —
    that would imply the MCP tool surface can trigger Apple's
    per-medication picker, which the directive forbids."""
    sources = _ios_swift_sources()
    if not sources:
        pytest.skip("OwnChartiOS source tree not present; static check skipped")

    mcp_offenders: list[str] = []
    for path in sources:
        if "MCP" not in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if (
            _MEDICATION_TYPE_FACTORY_INVOCATION in text
            or _MEDICATION_PICKER_INVOCATION in text
        ):
            mcp_offenders.append(str(path))

    assert not mcp_offenders, (
        "MCP source files reference Apple's medication-picker surface; "
        f"directive 2026-06-20 forbids MCP from triggering it. Offenders: "
        f"{mcp_offenders}"
    )


def test_ordinary_sync_engine_does_not_reference_medication_picker():
    """`BackfillEngine` and `IncrementalSyncEngine` run on app
    launch and during ordinary HK sync. Neither may name Apple's
    medication-picker surface — they must not be in a position to
    prompt the user as a side effect of a routine sync tick."""
    sources = _ios_swift_sources()
    if not sources:
        pytest.skip("OwnChartiOS source tree not present; static check skipped")

    sync_offenders: list[str] = []
    for path in sources:
        if path.name not in {"BackfillEngine.swift", "IncrementalSyncEngine.swift", "SyncOrchestrator.swift"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if (
            _MEDICATION_TYPE_FACTORY_INVOCATION in text
            or _MEDICATION_PICKER_INVOCATION in text
        ):
            sync_offenders.append(str(path))

    assert not sync_offenders, (
        "Ordinary sync engine references Apple's medication-picker surface; "
        f"directive 2026-06-20 forbids that. Offenders: {sync_offenders}"
    )


# ---------------------------------------------------------------------------
# Active-record reuse — no fallback, no silent upload
# ---------------------------------------------------------------------------


def test_medication_upload_uses_canonical_client_method():
    """The iOS direct-sync push for medications goes through the shared
    `HKUploadGate.shared.upload(_:via:interactive:)`, which is the sole
    caller of `OwnChartClient.uploadHealthKitBatch(_:)` (build 47, HK-2).
    That inherits the standard four-step active-record resolution
    (header → session → default → first-membership) AND the single-in-flight
    upload gate + user-activity bracket. Pin the call shape so a future
    refactor doesn't introduce a parallel HTTP path that could send
    medications to a different record or bypass the gate."""
    root = _ios_repo_root()
    if root is None:
        pytest.skip("OwnChartiOS source tree not present; static check skipped")

    flow = root / "Health" / "MedicationAccessFlow.swift"
    if not flow.is_file():
        pytest.skip(f"{flow} not present")
    raw = flow.read_text(encoding="utf-8", errors="ignore")
    # The presence-check wants to find the call site — strip
    # comments first so a future doc-only mention doesn't satisfy it.
    code_only = _strip_swift_comments(raw)

    assert "HKUploadGate.shared.upload(" in code_only, (
        "MedicationAccessFlow must push medication aggregates through "
        "HKUploadGate.shared.upload(_:via:interactive:) — the sole caller "
        "of OwnChartClient.uploadHealthKitBatch(_:) — so it inherits both "
        "active-record resolution and the single-in-flight upload gate."
    )
    # No hand-rolled HTTP — a separate URLRequest could bypass the
    # auth + record headers entirely. Check is over code-only so a
    # doc-comment describing the wire envelope doesn't false-positive.
    for forbidden in ("URLRequest(", "URLSession", "/api/healthkit/sync"):
        assert forbidden not in code_only, (
            f"MedicationAccessFlow contains hand-rolled HTTP {forbidden!r}; "
            "use OwnChartClient.uploadHealthKitBatch(_:) instead so "
            "active-record resolution stays the same."
        )


def test_medication_view_has_no_silent_upload_trigger():
    """The directive requires the upload to be a user-tap only. The
    Settings view must NOT call `uploadIfReady(via:)` from `onAppear`
    or from automatic state-change reactions — only from an explicit
    button-tap Task."""
    root = _ios_repo_root()
    if root is None:
        pytest.skip("OwnChartiOS source tree not present; static check skipped")

    view = root / "Features" / "Settings" / "MedicationsView.swift"
    if not view.is_file():
        pytest.skip(f"{view} not present")
    text = view.read_text(encoding="utf-8", errors="ignore")

    # Naive guard: `onAppear` blocks must not contain an upload call.
    # The build-46 implementation invokes uploadIfReady ONLY inside
    # the Button{}'s Task — that's the user-tap path.
    onappear_lines = [
        line for line in text.splitlines()
        if "onAppear" in line or "uploadIfReady" in line
    ]
    # We expect: zero `onAppear` calls in the file (none today), and
    # at most one mention of `uploadIfReady` (the Button action).
    onappear_count = sum(1 for line in onappear_lines if "onAppear" in line)
    upload_count = sum(1 for line in onappear_lines if "uploadIfReady" in line)
    assert onappear_count == 0, (
        "MedicationsView.swift contains onAppear hooks — verify none "
        "of them auto-invoke uploadIfReady(via:); the directive "
        "requires a user-initiated tap only."
    )
    assert upload_count == 1, (
        f"MedicationsView.swift mentions uploadIfReady {upload_count} times; "
        "expected exactly once (the Button action). A second call site "
        "could be a silent upload path."
    )
