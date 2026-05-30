# HealthKit MCP bridge

OwnChart can expose your iPhone's HealthKit data to a local AI agent on
your Mac (Claude Desktop, Claude Code, Codex, etc.) without any data
leaving your devices. The wire is a small open-source bridge process on
the Mac that talks to a local MCP server on the iPhone over Wi-Fi.

```
   Claude Desktop  ── stdio ──▶  ownchart-hk-mcp-bridge  ── HTTP ──▶  iPhone (OwnChart)  ──▶  Apple HealthKit
```

No cloud relay. No OwnChart backend in this path. Read-only. Paired with
a 6-digit code you read off the iPhone once; access can be revoked any
time from OwnChart Settings.

> **Scope of this release (Beta 1.1).** The bridge exposes
> aggregated HealthKit data — daily summaries via
> `healthkit_query_daily_summary` and a capability registry via
> `healthkit_capabilities`. It does **not** expose raw sample
> streams, GPS coordinates, workout routes, or medication dose
> events. It is a **local** integration with on-device MCP clients
> (Claude Desktop, Claude Code, Codex); ChatGPT remote-connector
> support is not implied and not provided. See the bridge repo for
> the full tool schema.

## Requirements

- **macOS** (Apple Silicon or Intel). Tested on macOS 14+.
- **Node.js 20+**, installed system-wide so `npm install -g` can
  drop the `ownchart-hk-mcp-bridge` binary on your `PATH`.
- **OwnChart iOS app** on a phone reachable over the same Wi-Fi
  network as the Mac. The bridge talks to the iOS app — the iOS
  app must be **running in the foreground** (with a brief grace
  period after you leave it). The bridge is not always-on
  background infrastructure.
- A Mac-side MCP client (Claude Desktop, Claude Code, or Codex)
  installed and configured to load MCP servers.

## Shortest install path

```sh
npm install -g ownchart-hk-mcp-bridge
ownchart-hk-mcp-bridge pair
```

Then add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ownchart-healthkit": {
      "command": "ownchart-hk-mcp-bridge",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Desktop. Two tools should appear in the inspector:
`healthkit_capabilities` and `healthkit_query_daily_summary`.

## On the iPhone

Open OwnChart → **Settings → Data & Ingestion → MCP server**. Toggle on.
The screen shows a 6-digit pairing code and the iPhone's LAN address.
Type the code into the Mac-side `pair` prompt once; the bridge remembers
it. Subsequent connections don't need re-pairing — only an explicit
"Forget paired bridges" revoke does.

The iOS server runs only while OwnChart is in the foreground (with a
brief grace period after you leave the app). To use the bridge, OwnChart
needs to be open.

## Where to go for everything else

The bridge has its own repo and npm package. They are the source of
truth for install, Claude Desktop config, troubleshooting, threat
model, and contract details — this page is the OwnChart-side
discovery surface, not a duplicate of the bridge README.

- **npm package:** <https://www.npmjs.com/package/ownchart-hk-mcp-bridge>
- **Source repo:** <https://github.com/nickpdawson/ownchart-hk-mcp-bridge>
- **Bridge `README.md`:** install, Claude Desktop config, smoke test,
  full troubleshooting matrix
- **Bridge `SECURITY.md`:** local-only threat model, logging guarantees,
  what the bridge does and does not defend against
- **Bridge `docs/ACCEPTANCE.md`:** acceptance test grid + release
  checklist

For the iOS-side contract (wire shapes, build history) see
`docs/HEALTHKIT_MCP_BRIDGE_SPEC.md` and `docs/HEALTHKIT_MCP_SPIKE.md`
in this repo.
