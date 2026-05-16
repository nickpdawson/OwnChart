# Network Access

> How to make OwnChart reachable — to a browser, to the iOS app, and
> to EHR vendors. The short version: HTTPS is non-negotiable for any
> real use, and the EHR side of the world cannot reach you over a VPN.

## The shape of an OwnChart deployment

```
                       ┌──────────────────────────────┐
  browser / iOS ─HTTPS▶│  reverse proxy (you provide) │
                       │  TLS termination + body size │
                       └──────────────┬───────────────┘
                                      │ HTTP, loopback or LAN
                                      ▼
                       ┌──────────────────────────────┐
                       │  web (8800) + api (loopback) │
                       └──────────────────────────────┘
```

Only the reverse proxy is on the public surface. OwnChart itself binds
the web container to `0.0.0.0:8800` (so the proxy can reach it) and
keeps `postgres`, `redis`, and `api` bound to loopback only.

## The non-negotiable: HTTPS

OwnChart marks session cookies `Secure` when `OWNCHART_ENV=prod`. That
means cookies will not be sent over plain HTTP at all, and the app
will appear broken. There are exactly two acceptable network postures
for real (non-trial) use:

1. **HTTPS via a reverse proxy you control.** Public DNS, a real cert
   (Let's Encrypt via NPM is the simplest path — see
   [REVERSE_PROXY.md](./REVERSE_PROXY.md)).
2. **HTTPS on a private network.** Tailscale Funnel, a private CA on
   your tailnet, a self-signed cert your devices trust — any path that
   gets you `https://` over a network you control.

Plain HTTP on the open internet is not supported.

`OWNCHART_ENV=dev` is for local trials only — it relaxes the `Secure`
cookie requirement so you can poke at `http://localhost:8800/`. Flip
it to `prod` the moment a real reverse proxy is in front.

## Three legitimate exposure choices

OwnChart is single-tenant patient infrastructure. Pick one:

### A. Public HTTPS (open internet)

Public DNS record, Let's Encrypt cert, reverse proxy in front of port
8800. Authentication still gates everything; the proxy and the app
together are your defense.

**Pros:** any browser, any iOS device, any EHR vendor can reach the
callback URL.

**Cons:** anyone on the internet can attempt to authenticate. Lock
auth down: `auth.allow_self_registration: false` (the default), strong
password, audit `model_runs` for unexpected calls.

### B. VPN / tailnet (private)

OwnChart sits inside a tailnet or VPN; only devices you've enrolled
can reach it. Tailscale, WireGuard, Headscale, ZeroTier — all fine.

**Pros:** no public attack surface.

**Cons:** **EHR OAuth callbacks will not work.** Epic, Athena, ModMed,
NextGen, and Cerner all redirect the browser back to your `redirect_uri`
**from the patient's device** — but the OAuth authorization step
itself runs from the user's browser through to your callback. If your
domain doesn't resolve publicly, the redirect succeeds (the browser is
on your VPN) but the EHR's authorization server **does not** make a
back-channel call to you. SMART-on-FHIR is browser-mediated, so VPN
deployment can work for the patient on the VPN.

The catch: some flows need an externally reachable HTTPS endpoint at
registration time (e.g., the EHR's developer console will validate the
domain). If you can't pass that, you can't ship.

**iOS app on VPN:** the iOS device has to be on the same VPN as the
server. Tailscale's iOS client handles this cleanly.

### C. Tailscale Funnel / Cloudflare Tunnel (hybrid)

Private origin, public hostname. Tailscale Funnel and Cloudflare
Tunnel both let you publish a TLS endpoint without opening a port on
your home router. The OwnChart container stays on a private network;
the tunnel terminates TLS at the edge.

**Pros:** no port-forwarding, public reachability for the iOS app and
for EHR callbacks, no exposed home IP.

**Cons:** the tunnel provider sees the encrypted bytes pass through.
Cloudflare can decrypt if you use their cert. Tailscale Funnel is
end-to-end as far as the public client → your origin goes. For PHI
infrastructure this is a trust trade-off worth thinking about.

**Cloudflare upload cap:** the free plan caps request bodies at 100 MB
regardless of your origin's `client_max_body_size`. See
[REVERSE_PROXY.md](./REVERSE_PROXY.md).

## Open ports — only if you understand the risk

If you're forwarding ports on a home router:

- Forward only **443** (and **80** for the ACME HTTP-01 challenge).
  Do not forward 8800 directly.
- Front 8800 with your reverse proxy on the same host (or a different
  host on your LAN).
- Block all other inbound traffic. The Postgres and Redis ports are
  loopback-only inside Compose by default; keep it that way.
- Audit your router's UPnP setting — disable it. Auto-opened ports
  defeat the firewall.

## EHR connector callbacks

Every SMART-on-FHIR connector OwnChart supports works the same way:

```
https://your-instance.example.com/api/connectors/callback
```

When you register your patient app with Epic, Athena, etc., you give
them this URL. The vendor stores it. On every connect attempt:

1. The patient's browser is redirected to the vendor's authorization
   server.
2. The patient logs in (MyChart, athenaPatient, etc.) and grants
   consent.
3. The vendor redirects the patient back to your callback URL with an
   authorization code.

The vendor never directly contacts your server. The callback runs in
the patient's browser. That means:

- If `your-instance.example.com` is not publicly resolvable, the
  callback fails on a vanilla browser.
- If the patient is on your VPN/tailnet, the callback works (the
  domain resolves on the VPN).
- Best path: a public hostname (cheap option: Cloudflare Tunnel,
  Tailscale Funnel, or a $12/year domain) so the OAuth flow works
  regardless of the patient's network posture.

## iOS app reachability

The TestFlight iOS app stores an `instanceBaseURL` you choose when
you pair it (e.g., `https://your-instance.example.com`). It sends all
HTTPS requests to `<instanceBaseURL>/api/...`. There is no `api.*`
subdomain in this contract — the single host serves both UI and API.

For the iOS app to reach your instance:

- The phone must resolve `your-instance.example.com`. Public DNS
  works. Tailscale iOS works. Private VPNs configured on the phone
  work.
- The phone must trust your cert. Let's Encrypt or any public CA cert
  works out of the box. A private CA (your home lab's) requires
  installing the root cert on the phone (Settings → General → VPN &
  Device Management).

## Decision sketch

```
Will EHR connectors be used?
├── Yes ─▶ Public HTTPS or Tailscale Funnel / Cloudflare Tunnel
│         (the vendor's authorization server must be reachable
│          from the patient's browser; private-only won't work
│          for vendor-side app registration validation).
└── No  ─▶ Private VPN / tailnet is fine. PDFs, HealthKit sync,
          screenshots, manual notes — all of these work without
          any public exposure.

Will the iOS app be used from outside your LAN?
├── Yes ─▶ Same answer as above; the phone has to reach the host.
└── No  ─▶ LAN HTTPS with a private CA is OK; install the cert on
          the phone.
```

## Quick verification

After whichever exposure model you picked:

```sh
# from a browser somewhere that should be able to reach the host:
curl -sfI https://your-instance.example.com/healthz

# from a browser somewhere that should NOT reach it (untrusted network):
curl -sf https://your-instance.example.com/healthz \
  && echo "your instance is reachable from this network — that may not be what you wanted"
```

If you intended a private deployment and the untrusted-network curl
succeeded, your firewall is wrong.
