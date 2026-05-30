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

The bridge has its own repo. It's the source of truth for install,
Claude Desktop config, troubleshooting, threat model, and contract
details:

- **Repo:** <https://github.com/nickpdawson/ownchart-hk-mcp-bridge> *(pending publish to npm; until then, see the bridge spec in `docs/HEALTHKIT_MCP_BRIDGE_SPEC.md`)*
- **README:** install, Claude config, smoke test, troubleshooting
- **SECURITY.md:** local-only threat model, logging guarantees, what the bridge does and does not defend against
- **docs/ACCEPTANCE.md:** acceptance test grid + release checklist

For the iOS-side contract (wire shapes, build history, what changed in
builds 38/39/40/42) see `docs/HEALTHKIT_MCP_BRIDGE_SPEC.md` and
`docs/HEALTHKIT_MCP_SPIKE.md` in this repo.
