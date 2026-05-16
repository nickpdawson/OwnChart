# Reverse proxy — install requirements

OwnChart runs the API + web app on a single origin and expects to be fronted by a TLS-terminating reverse proxy (nginx, Nginx Proxy Manager, Caddy, Traefik). This page lists the configuration requirements that aren't immediately obvious.

## Body-size limit — REQUIRED for photo, HEIC, PDF, voice uploads

iPhone photos (HEIC), scanned PDFs, voice memos, and ZIP exports routinely exceed nginx's default `client_max_body_size` of **1 MB**. Without raising this limit, the proxy will 413-reject the upload before it reaches OwnChart — and users will read "OwnChart is broken" instead of "your proxy needs a config knob."

### Recommended baseline

```
client_max_body_size 200m;
```

Applied to:

- **Nginx (raw)** — set inside the `server { ... }` block (or `http { ... }` for all hosts).
- **Nginx Proxy Manager (NPM)** — per host: Proxy Hosts → edit → Advanced tab → paste the directive. NPM persists this and re-generates the proxy host conf correctly.
- **Caddy** — `request_body { max_size 200MB }` directive.
- **Traefik** — middleware `buffering.maxRequestBodyBytes: 200000000`.

### Upstream caps you can't override

| Provider | Free-plan cap | Higher-tier cap |
|---|---|---|
| Cloudflare | **100 MB** | 200 MB (Pro), 500 MB (Business / Enterprise). |
| AWS API Gateway | 10 MB | 10 MB hard limit. |
| Fastly | 100 MB | Configurable on paid tiers. |

A `client_max_body_size 200m;` on your origin doesn't help if the CDN in front of you caps at 100 MB. Plan accordingly:

- **demo.ownchart.me** sits behind Cloudflare Free → 100 MB upper bound regardless of origin.
- **Self-hosted instances with direct DNS** → only the origin proxy limit applies.

### Why 200m, not larger

200 MB covers:

- iPhone HEIC bursts (~30–60 MB each)
- Multi-page scanned PDFs (50–150 MB)
- Compressed HealthKit exports (typically <100 MB)
- Apple Health Auto Export full-history JSON (typical 10–80 MB, sometimes 150 MB)

Going higher invites silent denial-of-service if a malicious uploader pushes a 1 GB blob. The OwnChart API has internal limits per content-type (`_PHOTO_MIN_BYTES = 8 KB`, planner payload cap 240k tokens, etc.), but the proxy is the first line.

## CORS

Don't.

OwnChart is single-origin by design. The API and web app share one host (`https://<instance>/`, `https://<instance>/api/*`), so no cross-origin request ever happens in normal operation. If your proxy adds `Access-Control-Allow-Origin: *` you've widened the trust surface for no benefit. Strip CORS headers at the proxy unless you have a specific multi-origin case (which you don't, for a single-tenant patient instance).

## Forwarded headers

OwnChart honors `X-Forwarded-Proto` and `X-Forwarded-For`. Make sure the proxy sets both correctly so OAuth callbacks and audit-log IPs are accurate.

For nginx-style configs:

```
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header Host              $host;
```

NPM templates do this by default.

## WebSocket / SSE

Not currently used. EI polling is HTTP. Reserve the standard `Upgrade` / `Connection` headers in proxy config so a future change doesn't require touching the proxy.

## Pre-alpha checklist

Before claiming an instance is alpha-ready:

- [ ] `client_max_body_size 200m;` (or the equivalent for your proxy) — confirm with `curl -F file=@a-50MB.jpg https://<instance>/api/sources/photo`.
- [ ] HTTPS only (HTTP → HTTPS redirect). OwnChart sets the `Secure` cookie flag in prod.
- [ ] HTTP/2 enabled if available (faster initial load on the SPA).
- [ ] Proxy logs go somewhere you can grep for forensic debug (audit log lives on the API; proxy logs help when the issue is "the request never arrived").
- [ ] If you're behind Cloudflare or an L7 WAF, verify it isn't rewriting the request body or stripping multipart boundaries.
- [ ] If using a CDN with a body-size cap below 200 MB, document that explicitly so users know not to upload videos / large HEIC bursts.
