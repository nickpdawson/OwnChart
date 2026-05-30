# HealthKit MCP Spike — Design Note

Created: 2026-05-27
Owner: `ios-shipping-engineer` (drafter) → PM (decision)
Status: **Design only.** No source code. Awaiting PM go for Phase 1 implementation.
Scope: **Beta 1.1 spike, post-Beta-1 release.** Explicitly out-of-scope for the Beta 1 release freeze.

## What this is

A local, user-authorized, **read-only** bridge from iOS HealthKit to a Model Context Protocol (MCP) tool surface. Lets a user's own AI agent (Claude Desktop on the same Wi-Fi, an on-device Shortcuts caller, a future local LLM) read the user's HealthKit data through structured MCP tools — **without going through OwnChart's sync / export pipeline**, and without the data ever touching OwnChart's backend.

The agent talks directly to the iPhone. OwnChart's server is not in the loop.

## What this is NOT

- **Not a replacement for OwnChart HealthKit sync.** The existing `SyncOrchestrator` / `BackfillEngine` / `IncrementalSyncEngine` path that POSTs to `/api/healthkit/sync` continues unchanged. The MCP spike is a *parallel* surface for ad-hoc queries by an external agent.
- **Not a cloud service.** No data leaves the phone via this path. The MCP transport binds to local network (or stdio over a companion later); no relay through `ownchart.dzsec.net` or any other backend.
- **Not an always-on silent data pipe.** The MCP server runs only when the user explicitly enables it in Settings AND the OwnChart app is foregrounded. Backgrounding or locking the phone stops the server (see §3.1).
- **Not writable.** No HealthKit writes through MCP, ever. The HKAuthorization request scope is read-only. (Apple's HK API permits read-only requests; we use that.)
- **Not the same as BE-6.** BE-6 (`Working Docs/BE-6_mcp_architecture_spec_2026_05_17.md`) is the *server-side* OwnChart-record MCP spec — queries against the Postgres record in the API container. This spike is *device-side* HealthKit MCP. The two are orthogonal: BE-6's tools are `search_records`, `get_fact`, `get_source`, etc. (record-stored extracted facts); this spike's tools are `healthkit.capabilities`, `healthkit.query_*` (raw HK aggregates). An agent could compose both at the client level. Neither implements the other.

## 1. Relationship to BE-6 and to the existing HK sync

| Surface | Talks to | Data shape | Auth |
|---|---|---|---|
| **OwnChart HK sync** (`POST /api/healthkit/sync`) | OwnChart API → Postgres | Server-curated `ExtractedFact` rows scoped to active person record | Device token (`X-OwnChart-Person-Record`) |
| **BE-6 OwnChart record MCP** (future) | OwnChart API → Postgres | Server-curated record content (facts, sources, episodes) | Per-(user, record) MCP token |
| **THIS spike: HealthKit MCP** | iPhone → iPhone HealthKit directly | Raw / aggregated HK samples in the moment | Per-pairing MCP token, local-network bound |

Each is a different trust posture. The HK MCP path is the most powerful because it bypasses OwnChart's record curation — agents get whatever HealthKit has on the device — which is exactly why it's gated on (a) the existing iOS HealthKit authorization the user already granted to OwnChart and (b) a new explicit "Enable MCP" toggle, with (c) a pairing code per session.

## 2. Phase 0 — transport feasibility on iOS

### 2.1 The transport landscape

**MCP wire protocol** is JSON-RPC 2.0 over either:
- **stdio** (the default for `claude-desktop`-spawned servers — parent process pipes stdin/stdout to a child binary)
- **HTTP + SSE** (Server-Sent Events for the streaming side)

On iOS:

| Option | Viable? | Notes |
|---|---|---|
| **A. Foreground HTTP+SSE server on the device** | ✅ **YES — recommended MVP** | Standard URLSession-backed HTTP server (or NIO-based) on a local port. Bonjour advertisement for discovery. Local Network permission required (iOS 14+). Works only while OwnChart is foregrounded. |
| B. stdio transport from iOS | ❌ NO | iOS doesn't permit external processes to spawn the app's binary or attach to stdio. No standard surface for it. |
| C. Background-mode server | ❌ NO | iOS doesn't permit arbitrary long-running daemons. BGTaskScheduler is for periodic ~30s wake-ups, not socket-serving. "Audio" background mode would be fraudulent. |
| D. Mac/desktop companion that reads HK via iCloud-Health sync | 🟡 LATER | macOS Sequoia+ can sync Apple Health to a Mac; a separate Mac-side MCP server could read it. But (a) requires the user to opt in to Mac sync, (b) HK on Mac is a subset of iPhone data, (c) latency to "right now" data is iCloud-sync-bound. Defer; revisit only if A is blocked. |
| E. Shortcuts as a transport adapter | 🟡 LATER | iOS Shortcuts can read some HK and hit local HTTP. A bridge could be built but it's a non-standard MCP shape. Defer. |

**Decision for the spike:** Option A. The MVP is a foreground local HTTP+SSE MCP server on the iPhone. The user opens OwnChart, taps Settings → "Enable MCP mode," gets a pairing code, configures Claude Desktop (or any MCP client) with `http://<iphone.local>:<port>` + the pairing token, and the agent can call the five tools listed in §3.

### 2.2 Constraints — Phase 0 ask, written down

These are the hard facts the spike has to operate within. Each one shapes the UX of the Settings toggle and the pairing flow.

**Foreground / background behavior (build 39, 2026-05-27 PM revision).**
- Server starts on Settings toggle; runs while OwnChart is in the foreground (state `.active`).
- On scene-phase background, the server transitions to `.gracePeriod` and asks iOS for extra execution time via `UIApplication.beginBackgroundTask(withName: "OwnChart MCP grace period")`. The listener keeps serving up to **5 minutes** (or whatever iOS actually grants — typically up to ~5 min on modern devices; sometimes less) after the user leaves the app. A paired bridge can complete a query the user kicked off seconds before backgrounding.
- Whichever fires first stops the server cleanly: (a) a 5-minute hard-stop timer the bridge code holds, or (b) `expirationHandler` from `beginBackgroundTask` when iOS is about to suspend. Both call `stop()` and `endBackgroundTask(_:)`. Never run past iOS's expiration.
- Re-foregrounding before expiration cancels the timer + ends the background task; state flips back to `.active`; the listener never stopped. Bridge calls in-flight at the moment of re-foregrounding succeed.
- Re-foregrounding AFTER expiration (rare — only if grace timer fired or iOS revoked) finds the server in `.idle`; the user has to re-toggle to start it again. Their paired-bridge tokens still work because pairing is persistent.
- App must NOT request `audio` / `location` / `BLE` / `VoIP` background modes to extend this. The `beginBackgroundTask` lane is available to every app without entitlements. App Store rejection territory and dishonest about scope; not done.
- Idle shutdown (5 min no traffic) still applies in parallel. If the user keeps OwnChart foregrounded but no bridge calls come in, the listener stops to save battery.
- **MCP Settings screen as an active-monitoring surface (build 42, 2026-05-30 PM directive).** While `MCPServerView` is the foreground view, the screen sets `MCPServer.isUIKeepAlive = true` and `UIApplication.shared.isIdleTimerDisabled = true`. The first pauses the idle-shutdown 30-second tick so the server stays up indefinitely; the second prevents the iPhone from auto-dimming and sleeping the screen (which would suspend networking despite the foreground state). Both reverse on `.onDisappear`. This matches the real ergonomic — the user is on this screen because they're debugging the bridge, paying attention to the pairing/state indicators; the "no traffic for 5 min, save battery" heuristic explicitly does not apply.

**Local Network permission (iOS 14+).**
- `NSLocalNetworkUsageDescription` must be added to `Info.plist`.
- `NSBonjourServices` array must declare the service types we advertise (e.g. `_ownchart-mcp._tcp.`).
- First time the app advertises Bonjour or accepts an inbound connection on the local network, iOS shows the system permission prompt. If the user denies, MCP mode reports "permission denied" and the toggle disables itself.
- Without local network permission, the server still binds the loopback port (`127.0.0.1`) so an on-device caller (like a Shortcuts action running on the same iPhone) could reach it; but Claude-Desktop-on-the-Mac use cases require local network grant.

**Device lock behavior.**
- iPhone locked + OwnChart in foreground: iOS still considers the app foregrounded for ~30 seconds, then puts it in a "background-ish" state where networking is suspended.
- The "MCP active" banner should warn the user that locking the device ends the session.
- Heuristic: if no MCP request arrives within 5 minutes, automatically stop the server. Saves battery and forces re-pair if the user walks away.
- Build 42 caveat: while the MCP Settings screen is foreground, the iPhone is held awake via `isIdleTimerDisabled = true` so the lock never fires on its own. The user can still manually lock the device; that follows the normal background path (grace period, then stop).

**TLS / auth / token story.**
- **Pairing model — persistent (build 38, 2026-05-27 PM directive):** when the user enables MCP for the first time and a bridge presents the 6-digit pairing code, the iPhone mints a long random session token AND persists a `MCPPairedBridge` record in the iOS Keychain. The record stores only the **SHA-256 hash** of the token (a verifier), never the plaintext. The plaintext token is returned to the bridge in the `/pair` HTTP response exactly once; the bridge stores it in **macOS Keychain** on its side. Subsequent `/mcp` calls present the plaintext token via `Authorization: Bearer`; the iPhone hashes inbound and constant-time compares against every stored verifier. **Toggling MCP off and back on does NOT invalidate paired bridges** — they survive across server restarts, app force-quit, and device reboot. Revocation is explicit: per-row revoke or "Forget paired bridges" in Settings.
- **Static TCP port (build 42, 2026-05-30 PM directive).** The listener binds to `MCPCaps.serverPort` (`52121`) on every start instead of letting the OS pick an ephemeral port. Rationale: ephemeral ports changed on every server restart and broke the bridge's cached `base_url` even with the build-40 server_id rediscovery flow — the bridge could find the iPhone again but had to re-resolve port-and-IP every time. With a static port, only the IP can shift (when the iPhone switches networks), so the cached `base_url` stays valid across MCP off/on cycles on a stable network. `52121` is in the IANA Dynamic / Private range (49152–65535), unlikely to collide with system services. If the port is already in use on the iPhone, the listener surfaces `.error` in the UI; the user can investigate. **The Bonjour `server_id` rediscovery flow (build 40) is still load-bearing for IP changes and multi-iPhone disambiguation** — see below.
- **Stable `server_id` for bridge reconnect (build 40, 2026-05-27 PM P0; retained under build 42).** Originally introduced because every server start bound a new ephemeral TCP port. Build 42 made the port static, but `server_id` is still required for (a) IP changes when the iPhone moves between networks, and (b) telling two OwnChart iPhones on the same Wi-Fi apart (Bonjour auto-appends " (2)" / " (3)" to the service name on collision, which is opaque to humans). The iOS server:
  - Generates a per-iPhone stable UUID `server_id` once and stores it in iOS Keychain (`MCPServerIdentity`, accessibility `AfterFirstUnlockThisDeviceOnly`, no iCloud sync).
  - Advertises it in the Bonjour service's TXT records: `server_id=<uuid>` + `server_name=ownchart-ios-mcp` + `server_version=0.1`.
  - Returns it in the `/pair` response: `{session_token, server_name, server_version, server_id}`. Old clients that ignore `server_id` still pair.
  - The bridge stores `server_id` alongside the paired-device record. On transport failure, it Bonjour-browses `_ownchart-mcp._tcp.`, matches the TXT `server_id`, updates its `base_url` to the current `host:port` (where `port` is now always `52121`), and retries. **The bridge does NOT clear its Keychain token on transport failure** — only on 401-revoked.
- **Optional client metadata in `/pair` (build 40).** The bridge may include `client_name` (e.g. the Mac's hostname), `client_kind` (`"mac"` / `"linux"` / `"windows"` / `"ios"`), and `bridge_name` (e.g. `"ownchart-hk-mcp-bridge"`) in the `/pair` request body. iOS stores these on the `MCPPairedBridge` record (sanitized, length-capped, no control chars) and shows `client_name` as the primary label in the paired-bridges list. The date moves to secondary metadata. Old bridge binaries that omit these fields still pair; their records show `"Paired bridge"` as the primary label.
- **Pairing code is still single-use + short-lived.** The 6-digit code generated when MCP starts has a 5-minute TTL, mints at most one paired-bridge record, and is rate-limited (≤5 attempts/minute) to defeat brute-force. Once consumed, MCP must be re-toggled to issue a new code for pairing another bridge.
- **Pairing must not affect server lifecycle (build 40 invariant).** The `/pair` handler does not call `stop()`, does not transition `state`, does not rebind the listener, does not change the port. Successful, failed, rate-limited, expired, already-paired, and storage-failure branches all leave the listener + port + state intact. Only explicit toggle-off, the idle timer, grace-period expiration, iOS background-task expiration, or app termination stop the server. (PM P0, 2026-05-27.)
- **TLS:** local HTTP is acceptable for the MVP if the pairing token is required on every request (defense against passive Wi-Fi sniffing depends on the local network being trusted — a doctrinally honest disclosure in the UX). For a tighter posture, generate a self-signed cert at pair time and have the client pin it from the pairing exchange.
- **No anonymous endpoints.** Every MCP method requires a token whose hash matches a stored `MCPPairedBridge.tokenHash`. The pairing exchange itself is the only unauthenticated endpoint and is rate-limited as above.

**Discoverability / reachability.**
- For Claude Desktop on a Mac to reach the iPhone, both must be on the same Wi-Fi (or via something like Tailscale that bridges the LAN). Cellular-only iPhone with Mac on a different network = no go for the MVP.
- Bonjour advertisement (service type `_ownchart-mcp._tcp.`) lets the MCP client discover the iPhone by name (`Nick's iPhone`) without typing IP.
- The Settings UI must surface the IP + port + Bonjour name so the user has fallback options if discovery is unreliable.

### 2.3 Library landscape

- **Official Anthropic Swift MCP SDK:** unknown at design time; if one exists when implementation starts, prefer it. Otherwise, implement minimal JSON-RPC 2.0 over HTTP+SSE inline — the wire format is small (a few hundred LoC) and pinning the framing ourselves avoids a transitive-dependency surface in a release-stabilization-adjacent piece.
- **HTTP server in Swift:** SwiftNIO is the idiomatic choice. For an MVP with one client at a time, a simpler `Network.framework` `NWListener` over `tcp` with a hand-rolled HTTP parser is also viable and ships with iOS. Decision at implementation time.
- **No third-party Bonjour library needed:** `NetService` / `NWBonjourServiceDescriptor` are sufficient.

## 3. Phase 1 — MCP tool surface

Five read-only tools. Each carries:
- **Input schema** (JSON Schema for the MCP `tool.inputSchema`).
- **Output shape** (the data the tool returns — also JSON Schema-able but listed here as Swift / wire shape).
- **Caps** (date range, row count, etc. — hard refusals beyond).
- **Failure modes** (refuse with structured error).
- **Privacy notes** (what's NOT returned by default).

### 3.1 `healthkit.capabilities`

What HealthKit types are authorized and reachable on this device.

**Input:** none.

**Output:**
```jsonc
{
  "device_model": "iPhone 16 Pro",     // UIDevice.current.model
  "ios_version": "26.x",                // UIDevice.current.systemVersion
  "authorized": [
    {"identifier": "HKQuantityTypeIdentifierStepCount",       "unit": "count",  "scope_label": "Steps"},
    {"identifier": "HKQuantityTypeIdentifierHeartRate",       "unit": "count/min", "scope_label": "Heart rate"},
    {"identifier": "HKCategoryTypeIdentifierSleepAnalysis",    "unit": null,    "scope_label": "Sleep"},
    {"identifier": "HKWorkoutType",                            "unit": null,    "scope_label": "Workouts"},
    ...
  ],
  "denied": [
    {"identifier": "HKCategoryTypeIdentifierMenstrualFlow",   "reason": "user_denied"}
  ],
  "not_determined": [],
  "server_version": "ownchart-ios-mcp/0.1"
}
```

**Caps:** none — this is a metadata call. Sub-100 LoC.

**Privacy notes:** identifiers and scope labels only. No values. Returns iOS device model so the agent knows the source.

**Reuse:** `HKTypeRegistry` (existing) for the enumeration; `HealthAuthorization` (existing) for status lookups.

### 3.2 `healthkit.query_samples`

Raw samples for one metric over a bounded window. **Default refuses if window > 30 days or row count > 5,000.**

**Input:**
```jsonc
{
  "identifier": "HKQuantityTypeIdentifierHeartRate",
  "start_at":   "2026-05-20T00:00:00Z",
  "end_at":     "2026-05-27T00:00:00Z",
  "limit":      1000,                       // default 500, max 5000
  "aggregation_preference": "raw"           // "raw" | "auto" — if "auto" and window > 7d, server may rewrite to daily summary and tell the caller
}
```

**Output (success):**
```jsonc
{
  "identifier": "HKQuantityTypeIdentifierHeartRate",
  "unit": "count/min",
  "sample_count": 1247,
  "samples": [
    {"start_at": "2026-05-20T00:00:14Z", "end_at": "2026-05-20T00:00:14Z", "value": 58, "source_name": "Apple Watch"},
    ...
  ],
  "window_start_at": "2026-05-20T00:00:00Z",
  "window_end_at":   "2026-05-27T00:00:00Z",
  "truncated": false                         // true if more samples exist beyond `limit`
}
```

**Output (refusal):**
```jsonc
{
  "error": "query_too_broad",
  "detail": "Requested window 73 days exceeds 30-day raw sample cap. Use healthkit.query_daily_summary or narrow the window.",
  "max_window_days": 30,
  "max_limit": 5000
}
```

**Caps:**
- Window ≤ 30 days.
- `limit` ≤ 5,000.
- Cumulative per-session quota: 50,000 raw samples returned in any rolling 5-minute window. Beyond → 429-style refusal.

**Privacy notes:** sample values are returned verbatim. Source name is included (per the `WorkoutSampleExtractor` `source` pattern from Slice 2). Bundle IDs and device serials are NOT returned.

**Reuse:** `HKQueryRunner.sampleQuery(...)` (existing), `BatchBuilder.clientSampleKey(...)` not needed (MCP wire doesn't dedup; that's the caller's problem).

### 3.3 `healthkit.query_daily_summary`

Daily-aggregated values for one or more metrics. The default tool for any window > 7 days.

**Phase 1.2 (build 41, 2026-05-27 follow-up):** sleep and workouts are first-class identifiers in this tool. They use `HKSampleQuery`-backed paths and emit shapes distinct from the scalar quantity shape — same wrapper tool, three metric shapes inside the `metrics` map. **No new tool surface.**

**Input:**
```jsonc
{
  "identifiers": [
    "HKQuantityTypeIdentifierStepCount",
    "HKCategoryTypeIdentifierSleepAnalysis",
    "HKWorkoutType",
    "HKQuantityTypeIdentifierHeartRate",
    "HKQuantityTypeIdentifierRestingHeartRate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierBodyMass",
    "HKQuantityTypeIdentifierBloodPressureSystolic",
    "HKQuantityTypeIdentifierBloodPressureDiastolic",
    "HKQuantityTypeIdentifierBloodGlucose"
  ],
  "start_at": "2026-04-27T00:00:00Z",
  "end_at":   "2026-05-27T00:00:00Z"
}
```

**Output:**
```jsonc
{
  "days": [
    {
      // Local-calendar day. Phase 1.2 unifies all metric kinds on local-day
      // keys: quantity rows by the HK statistics-bucket boundary; sleep by
      // **wake day** (local day of sample endDate — 23:00→07:00 sleep maps
      // to the morning, the way users index it); workouts by the local day
      // of startDate.
      "date": "2026-05-26",
      "metrics": {
        // Scalar quantity shape (Phase 1):
        "HKQuantityTypeIdentifierStepCount":         {"value": 8423,  "unit": "count",      "aggregation": "sum"},
        "HKQuantityTypeIdentifierHeartRate":         {"value": 68,    "unit": "count/min",  "aggregation": "avg"},
        "HKQuantityTypeIdentifierRestingHeartRate":  {"value": 52,    "unit": "count/min",  "aggregation": "avg"},
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": {"value": 42, "unit": "ms", "aggregation": "avg"},
        "HKQuantityTypeIdentifierActiveEnergyBurned":{"value": 612,   "unit": "kcal",       "aggregation": "sum"},
        "HKQuantityTypeIdentifierBodyMass":          {"value": 78.4,  "unit": "kg",         "aggregation": "latest"},
        "HKQuantityTypeIdentifierBloodPressureSystolic":  {"value": 118, "unit": "mmHg", "aggregation": "avg"},
        "HKQuantityTypeIdentifierBloodPressureDiastolic": {"value": 74,  "unit": "mmHg", "aggregation": "avg"},

        // Sleep shape (Phase 1.2). Integer minutes (round half-up).
        // Stage-specific keys are present only when iOS 16+ stages were
        // logged by the user's device. `sample_count` is post-merge —
        // informational only; not for user-facing display.
        "HKCategoryTypeIdentifierSleepAnalysis": {
          "asleep_total_minutes":       444,
          "in_bed_minutes":             486,
          "awake_minutes":              12,
          "rem_minutes":                95,
          "core_minutes":               220,
          "deep_minutes":               60,
          "asleep_unspecified_minutes": 69,
          "sample_count":               18,
          "unit":                       "min"
        },

        // Workout shape (Phase 1.2). One entry per local day. Activity
        // names match WorkoutSampleExtractor.stableActivityName (the same
        // ontology Slice 2 puts on the /api/healthkit/sync wire). No
        // source / device names, no per-event timestamps, no GPS routes.
        "HKWorkoutType": {
          "workout_count":            2,
          "total_duration_minutes":   72,
          "total_active_energy_kcal": 612.4,
          "total_distance_meters":    9420,
          "activity_breakdown": [
            {"activity": "running",            "count": 1, "duration_minutes": 42},
            {"activity": "strength_training",  "count": 1, "duration_minutes": 30}
          ],
          "unit": null
        },

        // No readings that day — present in the day row, null value.
        "HKQuantityTypeIdentifierBloodGlucose": null
      }
    },
    ...
  ],
  "window_start_at": "2026-04-27T00:00:00Z",
  "window_end_at":   "2026-05-27T00:00:00Z",
  // Identifier absent from per-day `metrics` map AND listed here →
  // either unauthorized, or the registry didn't recognize the string, or
  // the call hit `sample_volume_too_large` for that identifier. Null
  // value in `metrics` → identifier requested but no data that day.
  "missing_authorizations": ["HKQuantityTypeIdentifierBloodGlucose"]
}
```

Notes on the heterogeneous shape: callers distinguish by which keys are present on a metric value (`value` → scalar; `asleep_total_minutes` → sleep; `workout_count` → workout). No discriminator tag; the keys are disjoint by construction.

**Caps:**
- Window ≤ 366 days.
- Max 30 identifiers per call.
- Sleep / workout sample queries return ≤ 50,000 raw samples per identifier. Beyond → structured refusal `{"error":"sample_volume_too_large","max_samples_per_identifier":50000,...}`. Defensive; not reachable for sane windows.

**Privacy notes:** Aggregates only — no per-sample timestamps, no source names, no device IDs, no GPS routes. Sleep + workouts follow the same "aggregates only" doctrine as scalar quantity metrics; the day shape is the only granularity exposed.

**Reuse:** `HKQueryRunner.dailyAggregates(...)` (quantity), `HKQueryRunner.sampleQuery(...)` (sleep + workouts), `WorkoutSampleExtractor.stableActivityName(...)` for workout-activity canonicalization (no second ontology).

### 3.4 `healthkit.query_workouts`

Workout summaries. **GPS route points NOT returned by default.**

**Input:**
```jsonc
{
  "start_at": "2026-05-20T00:00:00Z",
  "end_at":   "2026-05-27T00:00:00Z",
  "workout_activity_type": "running"   // optional; stable string per Slice 2's WorkoutSampleExtractor
}
```

**Output:**
```jsonc
{
  "workouts": [
    {
      "id": "client-key",
      "start_at": "2026-05-25T15:00:14Z",
      "end_at":   "2026-05-25T16:00:23Z",
      "workout_activity_type": "running",
      "workout_activity_type_raw": 37,
      "duration_s": 3609,
      "distance_m": 7740,
      "energy_kcal": 542,
      "source": {"name": "Apple Watch", "version": "26.x"},
      "device": {"name": "Apple Watch", "model": "Watch7,1", "manufacturer": "Apple Inc."}
    },
    ...
  ],
  "window_start_at": "2026-05-20T00:00:00Z",
  "window_end_at":   "2026-05-27T00:00:00Z",
  "workout_count": 7,
  "route_data_available": true,                  // some of these workouts have GPS routes
  "route_data_returned": false                   // default — caller didn't request it
}
```

**GPS / route policy:** by default, route points are NEVER returned. A caller can request them only via a separate `include_route_points: true` flag, which iOS treats as a **separate consent path** — at the first such call, the iPhone shows a system-modal-style alert ("[Agent] is requesting your workout GPS routes. Allow this session?") and only proceeds on user-tap. This consent is per-session and not persisted.

**Caps:**
- Window ≤ 90 days for summaries.
- Window ≤ 14 days when `include_route_points=true`, max 5 workouts per call.

**Reuse:** `WorkoutSampleExtractor.stableActivityName(...)` and `.workoutMetadata(...)` (existing — exact same canonicalization Slice 2 uses on the wire to OwnChart's server).

### 3.5 `healthkit.explain_permissions`

What HealthKit access the user has actually granted to OwnChart, what's missing, and what the consequences are for MCP queries.

**Input:** none.

**Output:**
```jsonc
{
  "granted_count": 18,
  "denied_count": 2,
  "not_determined_count": 4,
  "granted": [
    {"identifier": "HKQuantityTypeIdentifierStepCount",    "category_label": "Steps", "added_at": "2026-05-12T20:14:00Z"},
    ...
  ],
  "denied": [
    {"identifier": "HKCategoryTypeIdentifierMenstrualFlow", "category_label": "Cycle tracking", "consequence": "MCP cannot return cycle data for this user."}
  ],
  "not_determined": [
    {"identifier": "HKQuantityTypeIdentifierBloodGlucose", "category_label": "Blood glucose", "consequence": "iOS hasn't prompted for this yet. Open Settings → HealthKit categories in OwnChart to grant."}
  ],
  "system_note": "Apple does not let any app know whether the user actively has no data of a given type vs. has denied permission silently. \"Denied\" here means OwnChart's read request was rejected, not necessarily that the user has no such data."
}
```

This tool is the "honest mirror" — it lets the agent give the user an accurate diagnosis when a query returns empty. Returns no health data, only permission state.

**Reuse:** `HealthAuthorization.statusFor(...)` (existing).

## 4. Privacy + safety contract

Hard rules. Enforced both in the tool implementations and surfaced in the Settings UI's "What MCP can do" explainer.

1. **Read-only.** No HealthKit writes through MCP, ever. The `HKHealthStore` request the spike makes is read-only (`requestAuthorization(toShare: nil, read: types)`).
2. **Explicit in-app MCP enable toggle.** Default OFF. Toggling ON opens the pairing flow. No silent server start.
3. **Pair-once persistent bearer token.** No anonymous local server. **Phase 1.1 (build 38, 2026-05-27):** one successful pair mints a paired-bridge record on iOS — token hash persisted in Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, `ThisDeviceOnly`, never the plaintext). The bridge stays authorized across MCP off/on, app launches, and device reboots. The 6-digit pairing code itself remains single-use, 5-minute TTL, and rate-limited. Revocation is explicit in Settings → MCP server → Paired bridges. _Phase 1 (build 37) used per-session tokens that died on every server stop; smoke validation found this forced re-pair on every toggle — addressed in 1.1._
4. **Default to aggregates over raw.** `query_daily_summary` is the recommended tool; `query_samples` is bounded and refuses broad windows.
5. **Hard caps on date range + row count.** Per-tool, listed above. Beyond → structured refusal with `max_*` in the error body so the caller can re-issue.
6. **Workout GPS / route data behind a separate consent.** Default off; per-session consent required. The Phase 1.2 daily-summary workout shape is aggregates-only — no route points, no per-event timestamps, no source / device names — irrespective of whether routes are authorized at the HK layer.
7. **Sleep + workout aggregates follow the same doctrine as scalar metrics.** Phase 1.2 (build 41) added sleep / workout daily aggregates to `query_daily_summary`. They emit integer-minute durations + counts only; no per-sample timestamps, no source / device names, no GPS, no raw values. Sleep sample-merging across devices is done before aggregation so two trackers logging asleep simultaneously can't double-count.
8. **No PHI or HealthKit data in logs.** Logger subsystem `OwnChart category mcp` debug-level logs may emit method name + identifier + sample count, never values, source names, or timestamps. Inspectable via Console.app for the user's own confidence.
9. **"Health data," "health metrics," explicit names** in user-facing UI. "Wearable" stays as dev shorthand only — same as the iOS parity contract §A3.
10. **Stop on background / lock.** Server stops on app backgrounding. In Phase 1.1, persistent paired-bridge records survive — the bridge resumes once the user foregrounds OwnChart and toggles MCP back on. No re-pair required.
11. **No analytics.** Zero telemetry on what queries were issued. The user enabled this, the user controls it.

## 5. Reuse from existing code

The spike does NOT fork a new HealthKit ontology. It rides on what's already in the app:

| Existing thing | Spike usage |
|---|---|
| `HKTypeRegistry` (server + iOS) | Source of truth for which identifiers exist, their units, preferred aggregations, and scope labels. |
| `HealthAuthorization` | Status lookups for `healthkit.capabilities` and `healthkit.explain_permissions`. |
| `HKQueryRunner.sampleQuery(...)` | Backs `healthkit.query_samples`. |
| `HKQueryRunner.dailyAggregates(...)` | Backs `healthkit.query_daily_summary`. |
| `WorkoutSampleExtractor.stableActivityName(...)` and `.workoutMetadata(...)` | Backs `healthkit.query_workouts`. Same canonicalization the existing Slice 2 sync uses on the wire to OwnChart's server. |
| `HKObjectTypeFactory` | Sample type lookup by identifier string. |
| Logger subsystems | `OwnChart category mcp` added (new subsystem; first new subsystem since `healthkit` and `api`). |

No new HealthKit type registry. No new authorization flow. The MCP tools are a thin wrapper that adds: transport, auth, caps, and structured output. If the existing sync engine learns new metrics (e.g. body temperature, audiogram), MCP picks them up automatically via the shared registry.

## 6. Settings UX

New screen: `Features/Settings/MCPServerView.swift` (or `Settings/MCP/`).

Lives under Settings → Data & Ingestion (sibling to Calendar sources, HealthKit categories).

**States:**

1. **Disabled** (default): Toggle off. One-paragraph explainer: "MCP lets a local AI agent on your Mac, like Claude Desktop on the same Wi-Fi, read your health metrics directly from this iPhone. OwnChart is not involved. Tap to enable."
2. **Enabling**: requesting Local Network permission, generating pairing code.
3. **Active**: showing
   - 6-digit pairing code (large, copy-tappable)
   - URL: `http://<bonjour-name>.local:<port>` (and IP fallback)
   - Bonjour service name
   - "Active session" indicator if a client has paired
   - "Stop MCP" button
4. **Active with session**: showing what the connected client has called recently (counts only — "queried daily summary 3 times in the last 5 minutes" — not what data).
5. **Denied permission**: surfaces the iOS Settings deep link.

**Banner:** while active, show a persistent in-app banner ("MCP server active — keep OwnChart foregrounded") that's visible on every other tab so the user doesn't forget it's running.

## 7. Implementation roster (Phase 1 + 1.1 + 1.2)

iOS source group `OwnChartiOS/.../MCP/` as of build 42:

| File | Purpose |
|---|---|
| `MCPServer.swift` | Lifecycle (start/stop), `NWListener` HTTP server, **static port binding (build 42)**, foreground/background hooks, grace period (Phase 1.1), `isUIKeepAlive` flag (build 42), JSON-RPC dispatch. |
| `MCPPairing.swift` | Pairing-code generation, token exchange endpoint, rate limiting; hands the SHA-256 token verifier to `MCPPairedBridgeStore`. |
| `MCPPairedBridge.swift` | Phase 1.1 paired-bridge record (Codable; hash-only). |
| `MCPPairedBridgeStore.swift` | Phase 1.1 Keychain-backed store; `match(bearer:)` does constant-time hash compare. |
| `MCPHealthKitTools.swift` | The two tools. **Phase 1.2:** dispatches quantity / sleep / workout per identifier; reuses `HKQueryRunner`, `WorkoutSampleExtractor.stableActivityName`. |
| `MCPModels.swift` | JSON-RPC envelopes + tool I/O. **Phase 1.2:** `HKMCPDailyMetricValue` sum type carrying scalar / sleep / workout shapes. |
| `MCPCaps.swift` | Hard caps + `dailySummaryMaxSamplesPerIdentifier = 50_000` (Phase 1.2 defensive safety) + **`serverPort = 52121` (build 42)**. |
| `MCPServerIdentity.swift` | Phase 1.1 stable server UUID for reconnect-by-discovery (separate doc). |

Modified files:

| File | Change |
|---|---|
| `Info.plist` | Add `NSLocalNetworkUsageDescription` + `NSBonjourServices` (`_ownchart-mcp._tcp.`). |
| `App/AppState.swift` | Add `mcpServer: MCPServer?` lifecycle alongside `syncOrchestrator` / `calendarCoordinator`. Spin up only when user-toggle is ON AND app is foregrounded AND not in demo mode. |
| `Features/Settings/SettingsView.swift` | Add NavigationLink to MCPServerView under Data & Ingestion. |
| `Features/Settings/MCPServerView.swift` | New — the UI described in §6. |

No backend changes. No `api/` touches. No migration. No public-doc changes outside this design note.

## 8. Acceptance criteria (Phase 1 PR — mirroring the directive)

1. ✅ This design note committed in `docs/`.
2. App can start / stop MCP mode in foreground via Settings.
3. MCP client (Claude Desktop, or `curl` for smoke) can call `healthkit.capabilities` and get a non-empty `authorized` list.
4. At least one aggregate query works — recommendation: 7-day daily summary for `HKQuantityTypeIdentifierStepCount` + sleep + `HKQuantityTypeIdentifierHeartRateVariabilitySDNN`.
5. Over-broad query (window > 30 days on `query_samples`) returns a structured refusal with clear `max_window_days` hint.
6. No data returned before HealthKit authorization on the app AND MCP pairing on the server.
7. No backend / API changes beyond a possible shared-doc reference (e.g. cross-link from BE-6 to here).

## 9. Risks + unknowns

- **iOS Local Network permission UX.** First-time prompt timing is critical. If we ask too early (e.g. on app launch), users default to "Don't Allow." Prompt should fire only when the user explicitly taps "Enable MCP," after a clear explainer screen.
- **App Store review angle.** Apple reviewers may flag "this iPhone runs a server other devices connect to" as a concern. Mitigation: read-only, user-controlled, foreground-only, with an in-app explainer that mirrors the privacy / safety contract. Spike scope is internal / TestFlight-bounded; App Store submission of this feature is a separate decision after Beta 1.1.
- **Lock-screen session loss.** Even with foreground exemption, lock will end sessions within seconds. User UX must be honest about this; agents that retry will get 401s and need to re-pair.
- **MCP wire churn.** Anthropic may evolve the MCP spec. The spike pins to the version current as of implementation date and surfaces the version in the `serverInfo` handshake; future spec bumps land as new versions of this spike.
- **Hard caps are first-guess.** The proposed caps (30d / 5,000 raw; 366d / 30 identifiers daily; 90d / unlimited workouts; 14d / 5 with routes) are starting points. Real usage data will adjust them.

## 10. Open questions for PM

1. **Phase 1 PR scope:** does PM want the full 5-tool surface in one PR, or just `capabilities` + `query_daily_summary` as a thinnest-possible spike with the other three deferred to a follow-up PR?
2. **Pairing UX:** 6-digit numeric code (proposed) vs QR-code-on-iPhone-scanned-by-Mac vs paste-the-token-into-Claude-Desktop's config. The numeric code is most familiar; QR adds a setup step but is more clearly user-mediated.
3. **TLS posture for MVP:** plain HTTP with token gate is the simplest path. Self-signed cert with pinning is doctrinally tighter but adds pairing complexity. PM preference?
4. **Settings tab placement:** under Data & Ingestion (alongside Calendar sources / HealthKit categories) feels right but elevates MCP visually as a "supported feature." Alternative: under "Advanced" / "Experimental" subsection to mark it as Beta 1.1.
5. **Telemetry / audit:** the privacy contract says "no analytics." Does PM want an in-app session log the user can review (counts of which tools were called, when) for their own confidence? Or zero local trail?
6. **What about other-record HealthKit?** OwnChart's multi-record model has per-record HK data on the server. MCP reads from the iPhone directly — the iPhone only knows one set of HK data (the device owner's). MCP returns whatever HealthKit has, regardless of which OwnChart record the user has active. Is that the right framing, or should MCP refuse when active record ≠ device owner's record (whichever that means)?

## 11. Out of scope (per directive)

- Cloud relay.
- Background always-on server.
- Route / GPS export (gated behind a separate consent if the caller asks; default off).
- Medical advice.
- Full OwnChart record access (BE-6's territory).
- Native EHR connector OAuth.

## 12. Cross-references

- **BE-6** (server-side OwnChart record MCP): `Working Docs/BE-6_mcp_architecture_spec_2026_05_17.md`. Orthogonal surface; see §1.
- **Slice 2 workout extractor** (canonical activity strings reused here): `OwnChartiOS/.../Health/WorkoutSampleExtractor.swift`.
- **iOS parity contract A3** (terminology — "health data" not "wearable"): `user-docs/IOS_PARITY.md` §A3.
- **Existing HK ingest contract** (the shape the OwnChart sync uses, which this spike does NOT change): `api/ownchart/ingest/healthkit_workout.py`.
- **PM directive** (this turn, 2026-05-27): "Start the HealthKit MCP server work as a scoped Beta 1.1 spike/implementation."

---

## Phase boundary

This design note completes Phase 0 acceptance gate #1 (design note describing transport choice and limitations).

**Awaiting PM approval to:**
- Commit this file (`docs/HEALTHKIT_MCP_SPIKE.md`).
- Start Phase 1 implementation per §7 (file roster) and §8 (acceptance criteria).
- Either resolve the §10 open questions or accept the proposed defaults.
