# Native HealthKit sync — iOS contract

The contract for native iOS sync against `POST /api/healthkit/sync`. This complements `UPLOAD_CONTRACT.md` (which covers the binary-upload paths) and the iOS parity doc.

## Endpoints (all under `/api/healthkit/`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/capabilities` | Identifier registry the iOS client keys off (per-identifier `scope`, `unit`, `strategy`). |
| POST | `/sync` | One HealthKit identifier per request, samples batched. |
| GET | `/sync/cursors` | Resume-after-reinstall: HKQueryAnchor blobs per identifier. |
| POST | `/sync/deletions` | Tombstone HK-deleted samples. (V1: stub.) |

## Authentication

Device-token bearer **or** session cookie. Use `get_user_from_device_token_or_session` — both code paths land at the same `User` record. Re-pair flow on 401 (see `UPLOAD_CONTRACT.md` retry rules).

## Sync request shape

```json
{
  "device_id": "<UUID>",
  "identifier": "HKQuantityTypeIdentifierHeartRate",
  "strategy": "daily_aggregate" | "raw",
  "samples": [
    {
      "client_sample_key": "agg-HKQuantityTypeIdentifierHeartRate-2026-05-15"
                          | "sha256(identifier|start|end|value)",
      "start_at": "ISO-8601 datetime",
      "end_at":   "ISO-8601 datetime",
      "value":    72.0,
      "display_text": "72 bpm",   // optional, server may regenerate
      "hk_uuid":   "<HKObject UUID>",  // optional; stored in coded_concepts, NOT the dedup key
      "source_name":      "Apple Watch",
      "source_bundle_id": "com.apple.health"
    }
  ],
  "anchor_blob": "<base64 HKQueryAnchor>",   // optional, for resume
  "mode": "demo" | "full"                     // demo caps batch + scopes
}
```

## Identifier coverage (HK_REGISTRY)

| Scope | Identifiers |
|---|---|
| **activity** | StepCount, DistanceWalkingRunning, ActiveEnergyBurned, BasalEnergyBurned, AppleExerciseTime, AppleStandTime, FlightsClimbed |
| **heart** | HeartRate, RestingHeartRate, HeartRateVariabilitySDNN, WalkingHeartRateAverage, VO2Max, BloodPressureSystolic, BloodPressureDiastolic, OxygenSaturation |
| **body** | BodyMass, BodyMassIndex, BodyFatPercentage, Height, LeanBodyMass, WaistCircumference |
| **sleep** | SleepAnalysis |
| **workouts** | HKWorkoutType, HKWorkoutRouteType |
| **nutrition** | DietaryEnergyConsumed, DietaryWater |
| **mindfulness** | MindfulSession |
| **symptoms** | Headache, Coughing, ... |

If the iOS app reads `GET /capabilities` first and only syncs identifiers the server advertises, no scope is dropped silently.

## Strategy enforcement

Server **rejects raw posts** for high-volume identifiers (HR, SpO2, steps, energy, etc.) outside `mode="full"`. iOS app must request `strategy="daily_aggregate"` for those. The capabilities response surfaces each identifier's allowed strategy so the client doesn't have to guess.

`StrategyRejected` → HTTP 422 with `detail` explaining which scope and what's allowed.

## Idempotency — `client_sample_key`

| Strategy | Key shape | Why |
|---|---|---|
| `daily_aggregate` | `agg-<identifier>-<YYYY-MM-DD>` | Date-keyed, source-neutral — collapses Apple Watch + iPhone duplicates for the same day. |
| `raw` | `sha256(identifier\|start\|end\|value)` | Content-derived — same reading from two devices collapses to one row. |

Partial unique index on `extracted_facts.client_sample_key` enforces. `ON CONFLICT DO NOTHING` lets retries be safe.

**Re-uploading the same anchor batch is always safe.** Network failure mid-batch → just send the whole batch again.

## Mode: demo vs full

| Mode | Batch cap | Raw posts | When |
|---|---|---|---|
| `demo` | 500 samples | Refused for heart/activity scopes | iOS alpha default. Keeps demo instance light. |
| `full` | 5000 samples | Allowed where the strategy permits | Production. Requires explicit operator flag. |

iOS must respect `BATCH_CAP` and chunk beyond it.

## Cursors — resume after reinstall

`GET /sync/cursors` returns the last `anchor_blob` per identifier seen on this device. After iOS app reinstall, the client reads this, decodes the `HKQueryAnchor`, and resumes sync from that point — no historical re-sync, no duplicates.

If the client doesn't supply `anchor_blob` on `POST /sync`, the server takes the full batch on its own terms (typically the iOS app's local cursor took precedence).

## Errors

Same contract as `UPLOAD_CONTRACT.md`:

| Status | When |
|---|---|
| 422 | StrategyRejected (raw post for an aggregate-only identifier). |
| 401 | Auth failed → re-pair flow. |
| 400 | Malformed body / unknown identifier. |
| 5xx | Catch-all returns structured `{"detail": ...}`; never plain text. |

## Alpha readiness

- ✅ Registry covers the scopes the iOS app needs.
- ✅ Idempotency keyed correctly per strategy.
- ✅ Cursor resume implemented.
- ✅ Demo-mode batch cap.
- 🟡 Deletion tombstoning is a V1 stub (POST `/sync/deletions` accepts but is a no-op). Post-alpha work; HK deletions are rare and not user-visible damage.
- 🟡 Backfill of historical samples from a fresh iOS install is **client-driven** — the app paces it. No server-side throttle yet; if the iOS app sends 100k samples in one batch, we'd hit BATCH_CAP and 422. Document as "respect BATCH_CAP" on iOS side.

## See also

- `UPLOAD_CONTRACT.md` — the binary-upload paths (photo, PDF).
- `IOS_PARITY.md` — single-origin contract.
- `api/ownchart/routes/healthkit_sync.py` — server source.
- `api/ownchart/ingest/healthkit.py` — registry + strategy enforcement.
