# Install / Deployment Guide

> OwnChart 0.1 alpha. Self-hosted only. Source-available under the
> [PolyForm Noncommercial License](https://polyformproject.org/licenses/noncommercial/1.0.0/).
> Read [RISK.md](./RISK.md) before pointing this at your own record.

This guide covers Docker Compose deployment on a single host. Reverse
proxy + TLS is a separate concern — see [REVERSE_PROXY.md](./REVERSE_PROXY.md).
Network exposure choices are in [NETWORK_ACCESS.md](./NETWORK_ACCESS.md).

## Prerequisites

- Linux host (Ubuntu 22.04+ or Debian 12+ are the tested baselines;
  macOS works for local trials, not recommended for the long-running
  instance). Disk-level encryption (LUKS, ZFS native, FileVault) is
  strongly recommended — OwnChart does not encrypt evidence at the
  application layer in 0.1.
- Docker Engine 24+ with the `compose` plugin.
- At least 4 GB RAM, 4 vCPU, 50 GB free disk to start. Evidence grows
  with what you ingest; FHIR + photos can comfortably hit hundreds of
  GB over a year of use.
- A reverse proxy (Nginx Proxy Manager, Caddy, nginx, Traefik) to
  terminate TLS. OwnChart does **not** terminate TLS itself.

## Services

`infra/docker-compose.yml` defines five services:

| Service | Image | Public? | Purpose |
|---|---|---|---|
| `web` | built from `web/Dockerfile` | **yes** (port `8800`) | Next.js UI; same-origin `/api/*` rewrite to api |
| `api` | built from `api/Dockerfile` | no (loopback `127.0.0.1:8801`) | FastAPI + Alembic migrations |
| `worker` | same image as `api` | no | Arq worker for long Claude-vision jobs |
| `postgres` | `pgvector/pgvector:pg16` | no (loopback `127.0.0.1:8802`) | application DB + pgvector + pg_trgm |
| `redis` | `redis:7-alpine` | no (loopback `127.0.0.1:8803`) | arq queue |

Only **port 8800** is exposed beyond loopback. Front it with HTTPS.

## Volumes

The compose file bind-mounts `../data/` (relative to `infra/`) into the
containers as `/data`. On a real deploy that means `/home/<you>/ownchart/data/`.
The `deploy.sh` script pre-creates these subdirectories so they are
owned by the host user (UID 1000), which matches the container UID:

```
data/
├── evidence/        # raw PDFs, page images, FHIR bundles, CCDA XML (content-addressed)
├── renders/         # derived previews
├── exports/         # user-initiated record exports
├── backups/         # operator-written DB dumps (you create these)
├── model_runs/      # raw LLM payload snapshots when audit-payloads is on
├── directories/     # vendor connector seed-derived directories
└── postgres/        # Postgres data dir (do not touch by hand)
```

The Postgres volume lives at `data/postgres/`. Do not delete it
casually — it holds the structured record (facts, episodes, audit
trail, OAuth tokens).

> **Container UID invariant.** The api and web containers run as UID
> 1000. On the host, the directories under `data/` must be owned by a
> UID-1000 user (typically the deploy user). The deploy script handles
> this; if you bootstrap by hand, `chown -R 1000:1000 data/` before
> first start.

## First-time setup

### 1. Clone and copy templates

```sh
git clone https://github.com/nickpdawson/OwnChart.git
cd OwnChart
cp infra/.env.example infra/.env
cp infra/config.example.yaml infra/config.yaml
```

### 2. Generate secrets in `infra/.env`

Edit `infra/.env`. The compose file refuses to start if any of these
are unset:

| Variable | How to generate | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | `tr -dc 'A-Za-z0-9_-' </dev/urandom \| head -c 32` | Tied to the Postgres data volume. Rotating it requires a Postgres `ALTER USER`. |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` | Rotating invalidates all sessions. |
| `OWNCHART_TOKEN_DEK` | `dd if=/dev/urandom bs=32 count=1 \| base64` | Encrypts stored OAuth refresh tokens. **Losing this invalidates every connected EHR.** Preserve across deploys; back it up offline. |

The `deploy.sh` helper script (for SSH-based deploys to a remote host)
generates all three the first time and preserves them across re-runs;
if you're running locally, generate them by hand.

Optional but useful:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Default API key for LLM features. Users can also bring their own via `/settings/providers` (BYOK). |
| `OWNCHART_AUTO_EXPORT_TOKEN` | Bearer the [Health Auto Export](https://www.healthyapps.dev/) iOS app sends to `/api/auto-export/push`. Generate with `tr -dc 'A-Za-z0-9_-' </dev/urandom \| head -c 48`. When unset, the endpoint returns 503. |
| `OWNCHART_EPIC_CLIENT_ID` / `_SANDBOX` | Set once you register an Epic patient app. See [EPIC_SETUP.md](./EPIC_SETUP.md). |
| `OWNCHART_ATHENA_CLIENT_ID` | See [ATHENA_SETUP.md](./ATHENA_SETUP.md). |
| `OWNCHART_MODMED_CLIENT_ID` | See [MODMED_SETUP.md](./MODMED_SETUP.md). |
| `OWNCHART_CERNER_CLIENT_ID` | Set once you register an Oracle Health (Cerner) Patient app at <https://code-console.cerner.com>. Public client (PKCE), no secret. See [CERNER_SETUP.md](./CERNER_SETUP.md). |
| `OWNCHART_GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | Enable Google Calendar connect in Settings -> Calendar. This uses a Google OAuth Web client, not an API key. See [GOOGLE_CALENDAR_SETUP.md](./GOOGLE_CALENDAR_SETUP.md). |
| `OWNCHART_DEBUG_PAYLOADS=true` | Logs raw request/response bodies. PHI risk. Off by default. |

### 3. Set the public base URL

The instance's public URL is the load-bearing setting for OAuth
redirects. Set it in `infra/.env`:

```sh
OWNCHART_PUBLIC_BASE_URL=https://your-instance.example.com
```

This is the URL you point your reverse proxy at. It composes the
OAuth `redirect_uri` you register with each EHR vendor:
`https://your-instance.example.com/api/connectors/callback`. There is
**no `api.*` subdomain** — OwnChart serves API and UI from the same
host. (See [IOS_PARITY.md](./IOS_PARITY.md) for the single-origin
contract.)

> **Env wins.** `infra/config.yaml` has an `instance.public_base_url`
> field that's parsed at startup but **not consulted by any route in
> 0.1 alpha** — only the `OWNCHART_PUBLIC_BASE_URL` env var is read.
> Setting both is fine (the env var wins); setting only the YAML
> leaves the live setting unset. This may change in a future release;
> for alpha, treat the env var as the live setting.

The rest of `infra/config.example.yaml` covers `auth.session_max_age_days`,
`llm.default_model`, `ingest.max_attachment_bytes`, `ingest.max_pdf_pages`,
`ingest.vision_extraction_enabled`, and `privacy.debug_payloads_default`.
All values have defaults; the file is optional.

### 4. Start the stack

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
```

The `api` container runs `alembic upgrade head` at start, so migrations
apply automatically. First-time build is 3–8 minutes depending on
hardware.

Health-check:

```sh
curl -sf http://localhost:8800/healthz && echo OK
```

### 5. First user / admin

OwnChart's account-creation model is **"first registration creates
the owner; everyone else needs an invite."**

- On a fresh DB, the **first** call to `POST /api/auth/register`
  succeeds. The new account is flagged `is_instance_admin=true`,
  gets a personal `person_record`, and an `owner` membership on
  that record — all in one transaction. This is the bootstrap
  path; no invite token is required and no separate setup step.
- Once any user exists, subsequent calls to `/api/auth/register`
  **require a valid `invite_token`** by default. Public
  unauthenticated registration is closed unless an operator
  explicitly opens it (see the `auth.allow_self_registration`
  knob below).
- Invites are owner-issued, single-use, hashed at rest, and
  expire after 24h / 7d / 30d (owner picks at creation). The
  owner copies the resulting URL out of band — there is no
  outbound email in 0.1.
- Multi-user / caregiver / household support is the Beta 1 use
  case the invite flow enables. See [SHIPPED_VS_ROADMAP.md](./SHIPPED_VS_ROADMAP.md).

**Creating the owner account.** Visit
`https://your-instance.example.com/` (or `http://localhost:8800/`
for a no-proxy local trial — set `OWNCHART_ENV=dev` in `.env` so the
session cookie isn't `Secure`-required) and click **First time?
Create the owner account.** on the login page.

**Inviting a family member or caregiver.** Sign in as an owner.
Go to **Settings → Records → New invite**. Pick:

- the invitee's email,
- whether they're joining one of your existing records (you pick
  the role — viewer / caregiver / owner) or creating their own
  new record (role locks to owner),
- expiry window.

Click **Create invite** and you'll see a URL once. Copy it and
send it to the invitee. The URL contains a token that lets them
register one new account against this invite; after that, the
URL stops working. If they don't accept before the expiry, or
you change your mind, click **Revoke** on the invite row.

**`auth.allow_self_registration`.** This knob in `config.yaml`
controls behavior of `POST /api/auth/register` calls that arrive
**without** an invite token, after the first user exists. Default
is `false`: register without invite → 403. If you set it to
`true`, registration without an invite is accepted, but the new
user lands with zero memberships and is routed to a "no records —
ask admin" recovery screen. That's almost never what you want; the
invite flow above is the recommended path.

## Backups

You back up two things. There is no built-in backup tool in 0.1.

### Postgres dump

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env \
  exec -T postgres pg_dump -U ownchart ownchart \
  | gzip > "data/backups/pg-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
```

Schedule daily via cron. Rotate to an encrypted destination off-host.

### Evidence directory

```sh
tar --create --gzip --file "data/backups/evidence-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
  data/evidence
```

`data/evidence/` is content-addressed — every blob is named by its
SHA-256. Backing it up is a straightforward file copy.

### Restore drill (do this once)

Stand up a fresh Compose stack on another host. Restore the Postgres
dump, restore `data/evidence/`, copy `infra/.env` (specifically
`OWNCHART_TOKEN_DEK`), and log in. A known-correct source should
resolve, and a known-correct EHR connection should still be usable —
this is the test that proves the DEK is preserved.

## Updating

```sh
git pull
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
```

Alembic runs again on api start; migrations are idempotent. Read
`user-docs/RELEASE_NOTES_ALPHA.md` (or the next release's notes) for
anything operator-affecting before upgrading.

> **Don't destroy `infra/.env`.** It's gitignored and survives `git
> pull` — that's the correct behavior. Never run `git clean -fdx` in
> this tree; losing `OWNCHART_TOKEN_DEK` invalidates every connected
> EHR's stored OAuth refresh tokens (you'd have to re-authorize every
> provider connection). Back up `infra/.env` offline along with your
> Postgres dumps.

## Troubleshooting startup

| Symptom | Likely cause | Fix |
|---|---|---|
| `set in infra/.env` error on compose up | `POSTGRES_PASSWORD`, `SESSION_SECRET`, or `OWNCHART_TOKEN_DEK` unset or still placeholder | Edit `infra/.env`, generate the missing value, redo `up -d`. |
| `api` container restarts in a loop with `permission denied: '/data'` | Host `data/` not owned by UID 1000 | `sudo chown -R 1000:1000 data/`. |
| Logins succeed in the UI but every subsequent request redirects to `/login` | Cookies marked `Secure` while you're testing over HTTP | Set `OWNCHART_ENV=dev` in `.env` and restart `api`. Flip back to `prod` once HTTPS is in front. |
| OAuth callbacks 4xx from the EHR | `OWNCHART_PUBLIC_BASE_URL` doesn't match what you registered with the vendor (the YAML `instance.public_base_url` is parsed but not consulted — only the env var is live in alpha) | Update `OWNCHART_PUBLIC_BASE_URL` in `infra/.env` so it matches the vendor registration byte-for-byte and restart the api container. |
| Photos / PDFs upload and 413 at the proxy | Reverse proxy body-size cap too low | See [REVERSE_PROXY.md](./REVERSE_PROXY.md): `client_max_body_size 200m;`. |
| EI runs forever, browser shows "thinking…" with no result | LLM call rate-limited or credit-balance exhausted | Check `model_runs` table for the latest row's error column; rotate `ANTHROPIC_API_KEY` or top up credits. |
| `arq` worker idle while extractions are stuck | Worker didn't start | `docker compose -f infra/docker-compose.yml --env-file infra/.env logs worker`; check redis connectivity. |

Logs:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env logs -f --tail=200
# or for a single service:
docker compose -f infra/docker-compose.yml --env-file infra/.env logs -f api
```

## What to set up next

- TLS in front of port 8800: [REVERSE_PROXY.md](./REVERSE_PROXY.md).
- Decide on network exposure: [NETWORK_ACCESS.md](./NETWORK_ACCESS.md).
- Connect a record: [CONNECTORS.md](./CONNECTORS.md) (start here) →
  per-vendor guide.
- Add the iOS companion: [IOS_PARITY.md](./IOS_PARITY.md), TestFlight
  at <https://testflight.apple.com/join/z8QemcTe>.
- Understand the risk model before pointing this at your own record:
  [RISK.md](./RISK.md).
