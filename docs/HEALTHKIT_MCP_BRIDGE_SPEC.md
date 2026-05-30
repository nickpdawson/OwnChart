# OwnChart HealthKit MCP Bridge — Desktop Component Spec

Created: 2026-05-27
Owner: PM → desktop-bridge developer (this spec is your handoff)
Companion document: `docs/HEALTHKIT_MCP_SPIKE.md` (the iOS-side design and tool surface).
Status: **Spec.** No reference implementation. iOS server side ships as Beta 1.1 build 37.

> **Release discipline (Beta 1.1).** This is an internal engineering
> contract. No public claims — marketing copy, docs-site mention, public
> roadmap update, README badge, blog post, or social — about the bridge
> or the HealthKit MCP feature land until **the bridge binary exists and
> passes the §13 acceptance tests on a real device.** Until then, the
> feature is internal-only. `docs-site` and the OwnChart README do not
> reference HealthKit MCP outside this and the companion spec.

## 1. What this component is

A small, local, user-installed program that runs on the user's Mac (initial target) and acts as the bridge between:

- **Above it**: a desktop MCP client — Claude Desktop, Codex, or any other MCP-conformant agent — that the user already has configured.
- **Below it**: the OwnChart iOS app's foreground-only HealthKit MCP server, running on the user's iPhone on the same Wi-Fi.

> **Phase 1.2 (build 41, 2026-05-27).** The iPhone server's `healthkit.query_daily_summary` output adds optional metric shapes for sleep (`HKCategoryTypeIdentifierSleepAnalysis`) and workouts (`HKWorkoutType`). The two-tool surface is unchanged; the bridge needs **no protocol changes** — it forwards the JSON body verbatim and Claude Desktop renders the new keys. See §7.4 for the exact wire-shape additions.

The bridge is a **client-of-iPhone, server-of-Claude-Desktop** pattern. It speaks **stdio + JSON-RPC 2.0** upward (the transport Claude Desktop already knows) and **HTTP/1.1** downward (the transport the iPhone implements per the spike note §2). Pairing happens once per iPhone-server session; the bridge then proxies tool calls.

```
   ┌────────────────────────┐
   │  Claude Desktop        │  (or any MCP client)
   │  / agent of choice     │
   └──────────┬─────────────┘
              │  stdio  /  JSON-RPC 2.0
              │  (spawned subprocess; standard MCP client transport)
              ▼
   ┌────────────────────────┐
   │  ownchart-mcp-bridge   │  ◄── THIS COMPONENT
   │  (your binary)         │
   │                        │
   │   - Bonjour discovery  │
   │   - Pair handshake     │
   │   - Token in Keychain  │
   │   - JSON-RPC proxy     │
   └──────────┬─────────────┘
              │  HTTP/1.1  /  JSON-RPC 2.0
              │  Authorization: Bearer <session_token>
              ▼
   ┌────────────────────────┐
   │  iPhone (OwnChart app) │
   │  foreground-only       │
   │  MCP server            │
   │  Bonjour: _ownchart-   │
   │           mcp._tcp.    │
   └──────────┬─────────────┘
              │  in-process HealthKit reads
              ▼
   ┌────────────────────────┐
   │  Apple HealthKit       │
   │  (HKHealthStore)       │
   └────────────────────────┘
```

## 2. What this component is NOT

- **Not a cloud service.** It runs entirely on the user's Mac. There is no OwnChart backend in the connection.
- **Not a long-lived background daemon.** It's spawned by the MCP client (Claude Desktop) the same way every other MCP server is — as a child process on demand. It exits when stdin closes.
- **Not an HTTP server.** It's an HTTP client to the iPhone. The only inbound surface it offers is stdio.
- **Not a HealthKit reader.** It carries no `HealthKit.framework` linkage. All HealthKit work happens on the iPhone. The bridge only forwards JSON-RPC.
- **Not a fallback path** when the iPhone is unreachable. If the phone is locked / backgrounded / off-network, the bridge returns clean errors. It does not cache, mirror, or synthesize HealthKit data.
- **Not a writeable surface.** The iPhone server is read-only (per the spike contract). The bridge is therefore also read-only by construction.
- **Not multi-tenant.** One bridge instance bridges one Claude Desktop session to one iPhone. Multiple iPhones require either a `--device` flag (recommended) or multiple bridge entries in the MCP client config.

## 3. Transport contract

### 3.1 Above the bridge — stdio + JSON-RPC 2.0

The MCP client (Claude Desktop) spawns the bridge as a subprocess and communicates over stdin / stdout per the MCP spec. The bridge:

- Reads newline-delimited JSON-RPC 2.0 messages from stdin.
- Writes newline-delimited JSON-RPC 2.0 messages to stdout.
- Logs to stderr only (never stdout — stdout is the MCP wire).
- Exits when stdin closes (clean shutdown path) or on SIGTERM.

Per the MCP spec the bridge implements at minimum:
- `initialize` (handshake)
- `notifications/initialized` (client-to-server notification)
- `tools/list`
- `tools/call`

Optional MCP methods that may appear in client traffic but are not load-bearing for this spike:
- `notifications/cancelled` — if the client cancels, forward to the iPhone if mid-call (best-effort; the iPhone tools are short-lived so usually too late).
- `ping` — respond with `{}`. Apple-style keep-alive.
- All other methods — return JSON-RPC `-32601 Method not found`.

### 3.2 Below the bridge — HTTP/1.1 + JSON-RPC 2.0

The iPhone server exposes two endpoints (per `docs/HEALTHKIT_MCP_SPIKE.md` §2.1):

| Endpoint | Method | Auth | Body |
|---|---|---|---|
| `/pair` | POST | none | `{"pairing_code": "<6 digits>"}` |
| `/mcp`  | POST | `Authorization: Bearer <session_token>` | JSON-RPC 2.0 request |

Plain HTTP/1.1 (no TLS in Beta 1.1 — see §11.1). Each request is one TCP connection, closed after the response. No SSE in the thinnest spike — the tools are synchronous and the iPhone responds with a single JSON body.

## 4. Discovery

Two paths, in order of preference:

### 4.1 Bonjour browse (recommended default)

Browse for the service type `_ownchart-mcp._tcp.` on the local link via the OS's mDNS resolver (`dns-sd` / `NetService` / `node-mdns` / `zeroconf` depending on language). For each discovered instance:
- Resolve to a host and port.
- **Read the TXT record** (build 40). Expected keys:
  - `server_id` — stable per-iPhone UUID. The bridge stores this on its paired-device record and matches it on rediscovery (see §5.7).
  - `server_name` — `"ownchart-ios-mcp"` (sanity check).
  - `server_version` — `"0.1"` (advisory; future versions add new TXT entries).
- Surface to the user with the instance name. The iPhone advertises the literal `"OwnChart MCP"` (no per-device name, no leak of the user's first name); multiple instances on one LAN get Apple-suffixed `"OwnChart MCP (2)"` etc.
- Prefer connecting via raw IPv4 from the resolved endpoint over the `.local` hostname — `.local` mDNS resolution is flaky on many Mac configurations (VPNs, custom DNS, corporate APs). The iOS Settings screen also shows the LAN IPv4 for the same reason.

If exactly one instance is found AND the bridge has at most one paired-device record (or its single record matches the TXT `server_id`), the bridge proceeds. If multiple instances are found, the bridge requires either a `--device <instance-name>` CLI flag OR a TXT `server_id` match to disambiguate.

### 4.2 Manual host:port (fallback)

When Bonjour resolution is unreliable (some VPN configurations break mDNS), the user can specify `--host <hostname-or-ip>` and `--port <port>` explicitly. The bridge skips Bonjour entirely and connects directly.

### 4.3 No discovery cache

The bridge does not persist discovered instances. Each launch starts fresh. The token is keyed by instance name + last-known endpoint (see §6) so a re-launch can re-use a token if Bonjour finds the same iPhone.

## 5. Pairing process

This is the heart of the spec. The pairing flow is **user-mediated** — the human reads a code off their iPhone and types it into a prompt — and is required **exactly once per bridge** (build 38, 2026-05-27 PM directive). The token survives iPhone-side server restarts, app force-quit, and device reboot until the user explicitly revokes the bridge in Settings.

### 5.1 Sequence

```
User                  Bridge (Mac)                  iPhone (OwnChart app)
 │                         │                              │
 │                         │  Bonjour browse              │
 │                         ├─────────────────────────────►│
 │                         │  service published           │
 │                         │◄─────────────────────────────│
 │                         │                              │
 │  open OwnChart →        │                              │
 │  Settings → MCP server  │                              │
 │  → toggle ON  ──────────┼──────────────────────────────►
 │                         │                              │
 │  iPhone shows           │                              │
 │  "Pairing code: 123-456"                               │
 │◄────────────────────────┼──────────────────────────────│
 │                         │                              │
 │  reads code             │                              │
 │  types code into bridge │                              │
 │  prompt  ───────────────►                              │
 │                         │  POST /pair                  │
 │                         │  {pairing_code: "123456"}    │
 │                         ├─────────────────────────────►│
 │                         │                              │
 │                         │  200 OK                      │
 │                         │  {session_token: "...",      │
 │                         │   server_name, server_version}│
 │                         │◄─────────────────────────────│
 │                         │                              │
 │                         │  store token in Keychain     │
 │                         │  (account: instance name,    │
 │                         │   service: ownchart-mcp)     │
 │                         │                              │
 │                         │  (all subsequent /mcp calls  │
 │                         │   use Authorization: Bearer) │
 │                         │                              │
```

### 5.2 First-launch prompt UX (stdio-friendly)

Claude Desktop's stdio transport is line-oriented JSON-RPC. There is no second channel for an out-of-band prompt. Two patterns the bridge can use:

#### Pattern A — pre-spawned interactive setup (recommended)

The bridge ships a separate CLI subcommand for first-time setup:

```sh
ownchart-mcp-bridge pair
```

When run interactively (TTY on stdin/stdout), this:

1. Browses Bonjour for `_ownchart-mcp._tcp.` services; prints a numbered list.
2. Prompts the user to pick one (or accepts a `--device` flag to skip).
3. Prompts: `Enter the pairing code shown on your iPhone (6 digits):`
4. POSTs to `/pair` with the code.
5. On success, stores the token in Keychain and prints `"Paired with <instance>. You can now point Claude Desktop at this bridge."`
6. On failure (wrong code / expired / rate-limited / already-paired), prints the error and re-prompts up to 3 times.

The user runs this once. After that, Claude Desktop's MCP server config can spawn the bridge without `pair`:

```sh
ownchart-mcp-bridge serve
```

— which loads the token from Keychain and proxies stdio↔HTTP. If no token is stored, `serve` prints a JSON-RPC error in the bridge's startup `initialize` response telling the client to instruct the user to run `ownchart-mcp-bridge pair` first.

#### Pattern B — single-binary auto-prompt (alternative)

The bridge has only one mode. On startup, if no token is in Keychain:
- It blocks on stdin for a special control message OR sends an MCP `notifications/message` upstream announcing the pairing requirement.
- This is less standard and Claude Desktop's UX for surfacing such notifications is incomplete as of this writing.

**Decision: Implement Pattern A.** The `pair` subcommand is a familiar Unix idiom and works against any TTY. Claude Desktop never spawns the bridge in a TTY (stdio is wired up to the parent process pipes), so the prompt cannot accidentally fire in production traffic.

### 5.3 The `/pair` request and response

Request (build 40 — extended with optional client metadata):

```http
POST /pair HTTP/1.1
Host: 10.0.0.42:54321
Content-Type: application/json
Content-Length: ~140

{
  "pairing_code": "123456",
  "client_name":  "Ridge",                       // optional; macOS hostname
  "client_kind":  "mac",                          // optional; "mac"|"linux"|"windows"|"ios"
  "bridge_name":  "ownchart-hk-mcp-bridge"        // optional; binary identifier
}
```

`pairing_code` is required. The other three are optional and additive — iOS sanitizes (trims, length-caps to 64 / 16 / 64, drops control chars) before persisting them on the `MCPPairedBridge` record. iOS Settings → MCP server → Paired bridges shows `client_name` as the primary label (e.g. `"Ridge"`), with `bridge_name` as a secondary fallback, and the legacy `"Paired bridge"` as a final fallback for old / curl-pair records.

**What the bridge MUST send for `client_name`:** the user-visible hostname only — `os.hostname()` in Node, `scutil --get ComputerName` shelled out, or the equivalent in your language. **MUST NOT** send:
- The OS username
- The Mac's serial number
- The device model (unless PM later authorizes)
- Anything PII-shaped (email, full name)

The user sees this string back on their iPhone. Misbehaving values are sanitized but should never have been sent.

Response (build 40 — extended with stable `server_id`):

```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: ~180

{
  "session_token":   "<64 hex chars>",
  "server_name":     "ownchart-ios-mcp",
  "server_version":  "0.1",
  "server_id":       "<UUID, stable per iPhone>"
}
```

`server_id` is the bridge's reconnection key (see §5.7). Store it alongside the session token. Old bridges that ignore the field still pair, but won't reconnect after the iPhone changes networks (its LAN IP shifts). The port is static (`52121`, build 42) so a stable network keeps `base_url` valid without `server_id` use.

The pairing code is a 6-digit numeric string. The bridge SHOULD strip any hyphens or whitespace the user types (the iPhone displays `"123-456"` but the wire form is `"123456"`).

**Backward compatibility:** iOS accepts `{"pairing_code": "..."}` alone (no client fields). Existing bridge code that doesn't supply client metadata pairs successfully; the paired-bridge record just shows `"Paired bridge"` instead of the hostname.

### 5.4 Pairing failure modes — wire shapes the iPhone returns

The bridge must surface each cleanly to the user (`pair` subcommand) or to the MCP client (subsequent `serve` calls that hit re-pairing):

| HTTP | Body shape | User-facing message |
|---|---|---|
| 200 | `{session_token, ...}` | "Paired with `<instance>`." |
| 400 | `{error: "invalid_body", detail}` | "The bridge sent a malformed pairing request. (Bug — report this.)" |
| 401 | `{error: "wrong_code"}` | "Wrong code. Check the iPhone screen and try again." |
| 409 | `{error: "already_paired", detail}` | "That code's already been used. Toggle MCP off and on in OwnChart to issue a new code." |
| 410 | `{error: "pairing_expired", detail}` | "That code expired (5-minute window). Toggle MCP off and on to issue a new code." |
| 429 | `{error: "rate_limited", detail}` | "Too many pairing attempts. Wait a minute and try again." |
| 503 | `{error: "server_not_active"}` | "OwnChart's MCP server isn't running. Open OwnChart and toggle MCP on." |
| (network) | n/a | "Couldn't reach the iPhone. Check Wi-Fi + that OwnChart is foregrounded." |

### 5.5 What happens after pairing

The bridge stores:

```
Service: ownchart-mcp-bridge
Account: <Bonjour instance name>   (e.g. "OwnChart MCP" or "OwnChart MCP (2)")
Generic password (the token): <64 hex chars>
```

…in the platform keychain (macOS Keychain via Security framework, or libsecret on Linux, or DPAPI on Windows). One token per device.

Every subsequent JSON-RPC call inbound from Claude Desktop becomes:

```http
POST /mcp HTTP/1.1
Host: iPhone.local:54321
Content-Type: application/json
Authorization: Bearer <session_token>
Content-Length: ...

<the JSON-RPC body from Claude Desktop, unchanged>
```

The bridge does NOT mutate the JSON-RPC body. Method names, params, ids — all pass through.

### 5.6 Token invalidation

Build 38 (2026-05-27 PM directive) makes pairing persistent across iPhone-side server restarts. The bridge's token remains valid in three ordinary scenarios:

- User toggles MCP off on the iPhone, then on again.
- User force-quits the OwnChart app, then re-launches.
- iPhone reboots.

In each of those, the bridge's next `/mcp` call after the server is back up succeeds without re-pairing. **The only paths that invalidate the token** are:

1. **Explicit user revoke** in Settings → MCP server: tap "Forget paired bridges" or swipe a single paired-bridge row to "Revoke."
2. **OwnChart app uninstall / re-install** (iOS clears app-scoped Keychain on uninstall).

When invalidation has happened (any of the above), the next `/mcp` call returns:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"error": "unauthorized", "detail": "Provide Authorization: Bearer <session_token>. Get a token from POST /pair."}
```

The bridge MUST:

1. Delete the stored token from Keychain.
2. Surface to the MCP client a JSON-RPC error on the in-flight call:
   ```json
   {"jsonrpc":"2.0","id":<id>,"error":{"code":-32000,"message":"OwnChart paired-bridge access was revoked. Run 'ownchart-mcp-bridge pair' to re-pair."}}
   ```
3. Continue forwarding subsequent calls as the same failure until re-paired. Do not auto-prompt for a code mid-session — Claude Desktop has no UI for that.

**Note for transient unreachability**, distinct from invalidation: if the iPhone is unreachable (server toggled off, off-Wi-Fi, grace period expired, iPhone rebooted but OwnChart not yet foregrounded), the bridge's TCP connection fails or times out — the iPhone returns nothing. That's a transport failure, NOT a 401. The bridge MUST distinguish these and use error `-32000` with the "unreachable" message (per §8), not the "revoked" message. Do not clear Keychain on transport failure — the token is still valid; the iPhone just isn't reachable right now.

### 5.7 Reconnection after iPhone port / network changes (build 40 + build 42)

**Why this section exists.** Originally (through build 41), iOS's `NWListener` bound a fresh ephemeral TCP port on every server start; the bridge's cached `base_url` (`http://10.x.x.x:<port>`) went stale on any MCP off+on cycle. **Build 42 (2026-05-30 PM directive) pinned the listener to a static port** — `MCPCaps.serverPort = 52121`. With the port fixed, only the IP can shift (when the iPhone moves between Wi-Fi networks or DHCP changes), so a stored `base_url` survives MCP off+on on a stable network. The Bonjour-based rediscovery flow below is still load-bearing for two cases: (1) the iPhone moved networks since the bridge last reached it, and (2) the bridge must tell two OwnChart iPhones apart on the same LAN. Persistent pairing (build 38) keeps the token valid across all of these — the bridge just needs to find the current IP.

**On every `/mcp` call the bridge issues:**

1. **Try the stored `base_url`.** Quick TCP connect + POST. If it succeeds (200 / 4xx / 5xx), use the response and update the stored `base_url`'s last-success timestamp.
2. **On transport failure** (TCP connection refused, timeout, DNS failure, network unreachable — anything where iOS did NOT respond with HTTP):
   1. **Do NOT clear the Keychain token.** Token is still valid.
   2. **Bonjour-browse `_ownchart-mcp._tcp.`** on the local network. Apply the same browse timeout you use for the `pair` subcommand.
   3. For each resolved service, read the TXT record. Look for a `server_id` key whose value equals the `server_id` stored with this paired-device record.
   4. **If exactly one TXT match**: update the stored `base_url` to the resolved service's current `host:port`, retry the original `/mcp` request once. If the retry succeeds, return its result. If the retry also transport-fails, surface as in step 2.5 below.
   5. **If zero TXT matches** (no service advertising this `server_id` on this LAN): the iPhone is unreachable. Return JSON-RPC error `-32000` with message `"OwnChart MCP server is not reachable. Open OwnChart on the paired iPhone and enable the MCP server."` Do not clear the token.
   6. **If multiple TXT matches** (two iPhones somehow advertising the same `server_id` — extremely unlikely barring a Keychain-restore scenario): refuse the call. Return JSON-RPC error `-32000` with message `"Multiple iPhones are advertising the same OwnChart MCP server_id; refusing to reconnect. Run 'ownchart-mcp-bridge unpair' and re-pair."` Do not auto-pick.

**Near-term workaround (until the bridge ships server_id-aware rediscovery):** if the bridge has only one paired-device record AND only one OwnChart MCP service is found via Bonjour, the bridge MAY use it without `server_id` matching. If two are found, refuse and require the user to re-run `ownchart-mcp-bridge pair` or pass `--base-url`. This is an honest stopgap, not the final shape — the `server_id` check is the durable solution.

**Distinguishing transport failure from revoke (build 40 reaffirmation):**

| Bridge observes | Meaning | Action |
|---|---|---|
| TCP connect / timeout / DNS error | iPhone unreachable | Bonjour rediscover (§5.7). Do not touch Keychain. |
| HTTP 401 with `{"error": "unauthorized"}` body | User revoked the bridge in iOS Settings, OR iOS app uninstalled | Clear Keychain token. Surface JSON-RPC `-32000` "re-pair" message. |
| HTTP 200 or 4xx (non-401) | Server responded | Honor the response. |

The bridge MUST distinguish these. A common bug class: treating any non-2xx as 401 and clearing the token unnecessarily. Don't.

**Background grace-period behavior (build 39, 2026-05-27 PM directive).** When the user backgrounds OwnChart or locks the iPhone with MCP running, the iOS server does NOT stop immediately. It enters a "grace period" of up to 5 minutes (or whatever iOS actually grants via `UIApplication.beginBackgroundTask` — typically a few minutes, never longer than 5). During grace, the listener keeps accepting `/mcp` calls and the bridge's token still works. From the bridge's perspective there's no observable state change — same TCP, same JSON-RPC, same 200 responses. After grace expires (timer or iOS revocation), the server stops; the next call fails with transport-failure semantics, not 401, because the token is still valid — the iPhone just isn't reachable. When the user foregrounds OwnChart again before they manually toggle MCP off, the server is in `.idle` (grace already ran out) — the user has to re-toggle MCP on, but the paired bridge does NOT need to re-pair. Same token still matches.

## 6. Token management

- **Storage:** OS-native secrets store. macOS Keychain (`Security.framework`), Linux libsecret (`gnome-keyring` / `kwallet`), Windows DPAPI.
- **Naming:** `service = "ownchart-mcp-bridge"`, `account = "<Bonjour instance name>"`.
- **Lifecycle:**
  - Written on successful `/pair`.
  - Read on every bridge startup (`serve`).
  - Deleted on confirmed 401 from `/mcp` (server restarted, token invalidated).
  - Deleted on explicit `ownchart-mcp-bridge unpair [--device <name>]` subcommand.
- **Never written to disk in plaintext.** Never logged. Never echoed back to the user.
- **One token per (account, device)** — there is no token rotation, no refresh token, no expiry inside the bridge's view of the world. The iPhone is the authority; if the iPhone says 401, the token is gone.

## 7. MCP method forwarding

### 7.1 `initialize`

Claude Desktop sends:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{...},"clientInfo":{"name":"claude-ai","version":"..."}}}
```

The bridge has two options:

**Option A — proxy through.** Forward to `/mcp`. iPhone responds with its `MCPInitializeResult` (`serverInfo.name="ownchart-ios-mcp"`, `protocolVersion="2024-11-05"`, `capabilities.tools={}`). Bridge returns that verbatim.

**Option B — answer locally.** Bridge holds the iPhone-side serverInfo cached from a recent ping; returns a bridge-augmented serverInfo (`name="ownchart-mcp-bridge"`, `version="<bridge version>"`).

**Decision: Option A.** Honest about who's actually serving. Claude Desktop's logs will reference `ownchart-ios-mcp` which is the truthful identity. Option B is reserved for a future hardening pass where the bridge can answer initialize before the iPhone is reachable (e.g. proactive `ownchart-mcp-bridge pair` prompt).

### 7.2 `notifications/initialized`

Claude Desktop sends, no id. The bridge forwards. iPhone returns empty body (the iPhone treats notifications synchronously over HTTP for transport simplicity — see spike note §2.3). The bridge forwards the empty response upstream OR returns nothing if Claude Desktop expects no response on notifications. Test against Claude Desktop's actual behavior; the safer move is to forward and let the upstream client ignore the response if it doesn't want one.

### 7.3 `tools/list`

Forward unchanged. iPhone returns the two-tool descriptor set (`healthkit.capabilities`, `healthkit.query_daily_summary`) with their JSON Schemas. Bridge passes through.

### 7.4 `tools/call`

Forward unchanged. iPhone returns the structured `MCPToolCallResult` with `content[0].text` carrying a JSON-stringified blob (capabilities snapshot OR daily-summary OR refusal payload) and `isError: bool`.

**Phase 1.2 (iPhone build 41) wire-shape additions** — the daily-summary `metrics` map can now carry three disjoint shapes per identifier. The bridge does not parse them; it forwards verbatim. Callers (Claude Desktop, etc.) distinguish by which keys are present:

- **Scalar quantity (unchanged from Phase 1):** `{"value": <number>, "unit": <string>, "aggregation": "sum"|"avg"|"min"|"max"|"latest"}`.
- **Sleep (`HKCategoryTypeIdentifierSleepAnalysis`, new):** `{"asleep_total_minutes": <int>, "in_bed_minutes": <int>?, "awake_minutes": <int>?, "rem_minutes": <int>?, "core_minutes": <int>?, "deep_minutes": <int>?, "asleep_unspecified_minutes": <int>?, "sample_count": <int>, "unit": "min"}`. Integer minutes (half-up). Stage-specific keys absent when the device didn't log iOS 16+ stages. `sample_count` is post-merge — informational, never user-facing verbatim.
- **Workout (`HKWorkoutType`, new):** `{"workout_count": <int>, "total_duration_minutes": <int>, "total_active_energy_kcal": <number>?, "total_distance_meters": <number>?, "activity_breakdown": [{"activity": <snake_case_string>, "count": <int>, "duration_minutes": <int>}], "unit": null}`. Activity names use the same `stableActivityName` ontology as the existing Slice 2 sync. No source / device names, no per-event timestamps, no GPS / routes.

A new refusal code `sample_volume_too_large` (with `max_samples_per_identifier`) can land in `content[0].text` when a hostile or pathological sleep / workout window would return > 50,000 raw samples. Defensive; should not be reachable for sane windows. Treat like any other `isError: true` refusal — surface to the user, do not retry blindly.

Wire-shape backwards compatibility: existing scalar metric shape is **unchanged** — Phase 1.1 / build 39 bridges keep working against build 41+. New keys are additive only.

### 7.5 Unknown methods

Bridge forwards. iPhone returns JSON-RPC error `-32601 Method not found`. Pass through.

## 8. Error mapping

The bridge translates network and HTTP failures into JSON-RPC errors visible to Claude Desktop. JSON-RPC error codes used:

| Code | Reserved-by-spec? | Bridge meaning |
|---|---|---|
| `-32700` | Yes | Parse error from upstream client |
| `-32600` | Yes | Invalid JSON-RPC envelope |
| `-32601` | Yes | Method not found (forwarded from iPhone) |
| `-32602` | Yes | Invalid params (forwarded from iPhone) |
| `-32603` | Yes | Internal error (forwarded from iPhone) |
| `-32000` | No (impl-defined) | Bridge-side wrapper: iPhone unreachable / token invalid / pairing required |
| `-32001` | No | Bridge-side wrapper: server returned a non-JSON HTTP error |

Per the JSON-RPC spec, codes `-32000` to `-32099` are reserved for implementation-defined server errors. The bridge uses `-32000` for environmental issues (the phone isn't reachable, the user hasn't paired yet) and `-32001` for transport-layer surprises.

When the bridge returns `-32000`, the `message` MUST be a short instruction the user can act on. Examples:

- `"OwnChart MCP server is unreachable. Make sure OwnChart is in the foreground on your iPhone and you're on the same Wi-Fi."`
- `"OwnChart MCP server restarted. Run 'ownchart-mcp-bridge pair' to re-pair."`
- `"No paired iPhone found. Run 'ownchart-mcp-bridge pair' to set one up."`

The bridge MUST NOT swallow errors silently and pretend tools succeeded. Empty responses on connection failures lead to silent data loss in the agent's reasoning.

## 9. Configuration surface

### 9.1 CLI

```sh
ownchart-mcp-bridge pair    [--device <instance-name>] [--host <host>] [--port <port>]
ownchart-mcp-bridge serve   [--device <instance-name>] [--host <host>] [--port <port>] [--timeout-seconds <n>]
ownchart-mcp-bridge unpair  [--device <instance-name>]
ownchart-mcp-bridge devices                                # list Bonjour-discovered + paired
ownchart-mcp-bridge version
```

Flags:

- `--device <name>` — pin to a specific Bonjour instance name. Required when multiple iPhones are reachable.
- `--host <hostname-or-ip>` and `--port <port>` — bypass Bonjour entirely. Both must be supplied together.
- `--timeout-seconds <n>` — HTTP timeout for `/mcp` calls. Default 30. Short tools (capabilities) finish in <1s; daily-summary across 366 days touches HK quietly and can take 5-15s.

### 9.2 Claude Desktop config snippet

The user adds to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "ownchart-healthkit": {
      "command": "/usr/local/bin/ownchart-mcp-bridge",
      "args": ["serve"]
    }
  }
}
```

If multiple iPhones are paired:

```json
{
  "mcpServers": {
    "ownchart-healthkit-nick-phone": {
      "command": "/usr/local/bin/ownchart-mcp-bridge",
      "args": ["serve", "--device", "OwnChart MCP"]
    },
    "ownchart-healthkit-test-phone": {
      "command": "/usr/local/bin/ownchart-mcp-bridge",
      "args": ["serve", "--device", "OwnChart MCP (2)"]
    }
  }
}
```

## 10. Implementation language guidance

**Recommended: TypeScript / Node.js.**

Reasons:
- Anthropic publishes `@modelcontextprotocol/sdk` for TypeScript with the most-maintained transport implementations (stdio, HTTP+SSE). Lots of reference MCP servers in the Claude ecosystem are TypeScript.
- `mdns` / `bonjour-service` for Bonjour browse.
- `keytar` for cross-platform keychain access (macOS Keychain, libsecret, DPAPI).
- Single binary distribution via `pkg` or `bun build --compile`.

**Alternatives:**

- **Swift** — if Mac-only and you want to ship a `.dmg` with a native preference pane. Trades cross-platform reach for tighter macOS integration.
- **Python** — `mcp` SDK available; `zeroconf` for Bonjour; `keyring` for keychain. Distribution via `pipx` or PyInstaller is less elegant than a single binary.
- **Go** — single-binary cross-compilation is the strongest. Pick if the developer is comfortable with the MCP wire-by-hand (no official Go SDK as of this writing).

**Not recommended:**
- Anything requiring a separate runtime install (JVM, .NET).
- Anything that requires the user to enable a browser to bridge (defeats the locality posture).

## 11. Security posture

### 11.1 TLS

**Phase 1: plain HTTP.** The iPhone server uses HTTP/1.1 with a per-pairing bearer token. On a trusted home Wi-Fi this is acceptable; the bridge inherits the same trust posture.

**Phase 2 (future):** if the iPhone advertises an `https://...` endpoint with a self-signed cert pinned at pair time, the bridge upgrades. The pair response would carry a `server_fingerprint` field; the bridge pins it and refuses to connect to a server whose cert doesn't match on subsequent `/mcp` calls. **Not in Phase 1.** Defer until the iOS spike confirms transport works.

### 11.2 What the bridge defends against

- **Casual LAN scanning.** Bearer-token gate stops random `curl /mcp` from a neighbor on the same coffee-shop Wi-Fi.
- **Token theft from disk.** Keychain protects against `cat ~/.ownchart-mcp-token`. There is no such file.
- **Replay of expired tokens.** iPhone-side invalidation on server restart + bridge's 401-triggered token delete close the window.

### 11.3 What the bridge does NOT defend against

- **Local-machine attacker with root.** A privileged process on the user's Mac can read the bridge's stdout and the in-memory token. This is the standard threat model for any MCP server; out of scope.
- **Active MITM on the LAN.** Without TLS, a same-LAN attacker can capture and replay JSON-RPC bodies (and the token in flight). The Phase-2 TLS upgrade closes this; the Phase-1 mitigation is "don't run this on hostile Wi-Fi." Document the limitation in the bridge's README.
- **Compromised iPhone.** The bridge trusts whatever the iPhone server returns. If the iPhone is jailbroken or running a malicious build, all bets are off.

### 11.4 No telemetry, no analytics

The bridge MUST NOT phone home. No update checks against a public server unless explicitly opted in. No usage counters. No crash reports beyond stderr logs to the user's machine.

## 12. Logging

- **stdout: JSON-RPC traffic only.** Anything else there breaks the MCP client.
- **stderr: bridge-internal log lines.** Format `[YYYY-MM-DDTHH:MM:SSZ] level=info component=<x> ...`. Never log:
  - Pairing codes
  - Session tokens
  - JSON-RPC request bodies
  - JSON-RPC response bodies
  - Any field whose path includes `"value"`, `"sum"`, `"avg"`, `"days"`, `"metrics"`
- **stderr SHOULD log:**
  - Bridge startup + version + detected device name (no IP).
  - HTTP request shape (method + path + status code; never body).
  - JSON-RPC method name + duration_ms + result_is_error.
  - Errors with category but not content.
- **Verbose mode (`OWNCHART_MCP_BRIDGE_LOG=debug`)** may add tool call counts and discovery details; still never PHI / values.

## 13. Acceptance tests

When the developer hands the binary back, these must pass against a real paired iPhone running OwnChart build 37+:

1. **`pair` end-to-end.**
   - User runs `ownchart-mcp-bridge pair`.
   - Tool browses Bonjour; finds at least one `_ownchart-mcp._tcp.` instance.
   - Prompts for code; user types 6 digits.
   - POSTs `/pair`; receives token; stores in Keychain.
   - Prints "Paired."
2. **`pair` failure modes** map to the §5.4 table. Each error code surfaces the right user-facing message; the bridge does not crash or write a token on failure.
3. **`serve` proxies `initialize`.**
   - Claude Desktop connects; `initialize` response carries `serverInfo.name = "ownchart-ios-mcp"`.
4. **`serve` proxies `tools/list`.**
   - Returns exactly two tools: `healthkit.capabilities` and `healthkit.query_daily_summary`.
5. **`serve` proxies `tools/call healthkit.capabilities`.**
   - Returns content[0].text JSON with `authorized` / `denied` / `not_determined` arrays.
6. **`serve` proxies `tools/call healthkit.query_daily_summary`** for 7 days steps + HRV + RHR.
   - Returns content[0].text JSON with 7 daily rows; each carries `metrics.HKQuantityTypeIdentifierStepCount.value` and `metrics.HKQuantityTypeIdentifierHeartRateVariabilitySDNN.value` (when iPhone has them).
7. **Refusal forwarding.** `tools/call query_daily_summary` with a 400-day window → `isError: true` + `error: "window_too_broad"` in content[0].text.
8. **Token-invalidation recovery (build 39 — explicit revoke only).** User taps "Forget paired bridges" in OwnChart Settings → MCP server (NOT the off-toggle — that no longer invalidates per build 38+). Next `tools/call` returns JSON-RPC `-32000` with the "revoked" message; Keychain token is cleared. The bridge MUST distinguish revoke (401 from `/mcp`) from transient unreachability (transport failure) and use the right `-32000` message.
9. **MCP off/on cycle preserves pairing (build 38+).** User toggles iPhone MCP off then on again from Settings. The bridge's stored token still works — next `tools/call` succeeds without re-pair. (This is the persistent-pairing acceptance.)
10. **Background grace period (build 39).** Bridge is paired, user backgrounds OwnChart on iPhone, bridge immediately issues `tools/list`: succeeds (grace period is active, listener still up). Wait beyond grace (~5 min, or trigger iOS expiration by force-quitting the app): next `tools/list` fails as transport-unreachable, NOT as 401 — token is still valid, the iPhone just isn't reachable.
11. **Foreground recovery after grace expiry.** User foregrounds OwnChart and re-toggles MCP on. Same paired bridge, same Keychain token: next `tools/list` succeeds without re-pair.
12. **iPhone unreachable (other).** Off-Wi-Fi / cellular-only / OwnChart not foregrounded and grace already expired. Next `tools/call` returns JSON-RPC `-32000` with the "unreachable" message; the bridge does NOT crash, hang, or wedge stdin.
13. **`unpair`.** Removes the Keychain entry; `serve` then refuses to start until `pair` is re-run.
14. **Server-restart reconnect (build 40 + build 42).** Bridge is paired. User toggles MCP off, then on again. With build 42's static port, the iPhone comes back up on the same `host:port` and the bridge's stored `base_url` works on the first call — no rediscovery needed on a stable network. **Network-change variant (the case `server_id` still solves):** the iPhone moved Wi-Fi networks since the bridge last reached it (LAN IP shifted). Next `tools/call`: stored `base_url` transport-fails → Bonjour browse → TXT `server_id` matches the bridge's stored value → `base_url` updated to current `host:52121` → retry succeeds. No re-pair required.
15. **Pairing does not stop the server (build 40).** Bridge POSTs `/pair` with a wrong / expired / rate-limited / storage-failed code. iPhone-side state, listener, and port are unchanged. A successful pair leaves the server still on the same port. Verify by inspecting iOS Settings → MCP server → "Server: On" indicator before and after the pair attempt.
16. **Paired-bridge primary label (build 40).** Bridge sends `client_name: "Ridge"` in `/pair`. iOS Paired bridges list shows `"Ridge"` as the primary label, with `"Paired <date> · Last seen <datetime>"` as secondary metadata. An old binary that omits `client_name` shows `"Paired bridge"` instead.
17. **Ambiguous rediscovery (build 40).** Two iPhones somehow advertising the same `server_id` on the LAN → bridge refuses to auto-pick. JSON-RPC `-32000` with the "multiple iPhones" message. (Hard to reproduce in practice; spot-test by spoofing a second mDNS advertiser with the same TXT.)
18. **`devices`.** Lists Bonjour-discovered instances and indicates which (if any) have a stored Keychain token (matched by `server_id` in TXT).
19. **Stdout cleanliness.** No bridge-internal log lines on stdout. Verified by `ownchart-mcp-bridge serve < /dev/null > /tmp/out.txt 2>/tmp/err.txt; cat /tmp/out.txt` — empty or pure JSON-RPC.
20. **Cross-platform sanity** (if multi-platform target): bridge runs the same on macOS, Linux (libsecret), and Windows (DPAPI). Defer Linux/Windows if Mac-first is acceptable.

## 14. Non-goals (explicit)

- **Claude Desktop installer.** The bridge does not modify Claude Desktop's config or install it; the user does that manually with the snippet from §9.2.
- **iPhone-side changes.** All iOS server work is in `docs/HEALTHKIT_MCP_SPIKE.md`. The bridge is a pure client.
- **OwnChart backend integration.** The bridge never talks to `ownchart.dzsec.net` or any other OwnChart server.
- **HealthKit value transformation.** Whatever the iPhone returns is what Claude Desktop sees. No unit conversion, no rounding, no aggregation in the bridge.
- **Multi-record awareness.** OwnChart's multi-record model is server-side. The iPhone exposes one device's HealthKit data; the bridge doesn't care which OwnChart record the user has active. Documented in the iOS spike note §10.6.
- **Background scheduling / cron-style queries.** The bridge is request-response. Persistent watch / push semantics are out of scope.
- **Caching.** The bridge has no cache. Each `tools/call` is a fresh trip to the iPhone.

## 15. Open questions

1. **Bridge installer / distribution.** Homebrew formula? Direct download from a release page? GitHub release with codesigned `.pkg`? Pick a path during implementation; not blocking for the dev to start.
2. **MCP protocol version.** The iPhone advertises `"2024-11-05"`. If a future iPhone build bumps this, the bridge must either negotiate down or refuse. Document the version handshake explicitly.
3. **Pair subcommand UX in TTY-less environments.** A user who launches Claude Desktop without ever having run `pair` interactively gets a non-actionable error from the bridge. Acceptable for v0.1; consider an option to launch `ownchart-mcp-bridge` GUI from a system-tray menu in v0.2.
4. **Self-signed TLS upgrade path.** Needs spec text on the iPhone side first (§11.1 Phase 2). Coordinate via a future addendum.
5. **Should the bridge expose its OWN tools** — e.g. `bridge.status` that reports the connected iPhone + last call time — or stay purely a pass-through? Recommend pure pass-through for v0.1; revisit if Claude Desktop UX needs bridge-side observability.

## 16. Cross-references

- iOS-side spike note (canonical contract): `docs/HEALTHKIT_MCP_SPIKE.md`
- Phase 1 implementation handoff (iOS source roster): `Working Docs/BETA11_HK_MCP_PHASE1_HANDOFF_2026_05_27.md`
- Sleep / category-type follow-up: `Working Docs/FU-HK-MCP-CATEGORY-SUMMARIES.md`
- MCP spec: https://spec.modelcontextprotocol.io/specification/2024-11-05/
- Anthropic TypeScript SDK: https://github.com/modelcontextprotocol/typescript-sdk
- macOS Keychain Services (Security framework) reference: https://developer.apple.com/documentation/security/keychain_services
- Apple Bonjour (`_<service>._tcp.`) overview: https://developer.apple.com/bonjour/

## 17. What the developer should produce

- **One binary** (or platform-specific binaries) named `ownchart-mcp-bridge`.
- **A short README** describing install + first-run pairing + Claude Desktop config snippet, plus the "don't run on hostile Wi-Fi" disclosure from §11.3.
- **Acceptance test results** for each of the 20 items in §13, run against a real iPhone with OwnChart build 42+ (build 42 pins the listener to static port `52121` and keeps the iPhone awake + server alive while the MCP Settings screen is foreground; build 41 added sleep / workouts to `query_daily_summary`; build 40 introduced stable `server_id` + Bonjour TXT records, client-metadata in `/pair`, and the pairing-doesn't-stop-server invariant; build 39 brought background grace and persistent pairing).
- **No code in this repo.** The bridge lives in a separate repo named **`ownchart-hk-mcp-bridge`** (PM-assigned 2026-05-27). The OwnChart app repo carries this spec and the iOS server; it does NOT carry bridge source.
- **License: MIT or Apache-2.0.** Either is acceptable; pick one and stick with it. OwnChart-app proper ships under PolyForm Noncommercial 1.0.0 — that license does NOT apply to the bridge, because the bridge is a pure local protocol relay with no OwnChart product code. Permissive licensing is appropriate so users and other agents can inspect, install, and (if motivated) fork the bridge without friction.

## 18. Sequence summary (quick reference)

```
1. Developer: ships binary.
2. User installs binary + adds Claude Desktop config snippet.
3. User opens OwnChart on iPhone → Settings → MCP server → toggle ON.
   iPhone displays a 6-digit pairing code.
4. User runs `ownchart-mcp-bridge pair` in a Terminal.
   Bridge browses Bonjour, prompts for code.
5. User types the code. Bridge POSTs /pair → 200 → token stored in Keychain.
6. User opens Claude Desktop. Claude spawns ownchart-mcp-bridge serve.
   Bridge proxies stdio↔HTTP.
7. User asks Claude: "What were my steps last week?"
   Claude → bridge → iPhone → HealthKit → iPhone → bridge → Claude → user.
8. User backgrounds OwnChart. Next call → -32000 -> Claude shows
   "OwnChart MCP server is unreachable." User foregrounds + re-pair.
```

That's the complete loop.
