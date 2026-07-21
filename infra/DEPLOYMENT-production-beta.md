# OwnChart — Production (Beta) Deployment & Operations Guide

**Environment:** Lapland (Hetzner Helsinki)  ·  **Public URL:** <https://app.ownchart.me>  ·  **Stood up:** 2026-07-12

This is the handoff doc for deploying, updating, and operating the OwnChart
**production (beta)** instance. "Multitenant" here means **one stack / one
Postgres serving many app users & tenants** — not per-tenant stacks.

> Audience: OwnChart dev/ops. Everything you need to ship a release and keep it
> healthy is here. No secret values are in this document.

---

## 1. Request path (how a browser reaches the app)

```
Browser
  │  https (edge TLS = Cloudflare cert)
  ▼
Cloudflare (proxied A record: app.ownchart.me)
  │  https to origin
  ▼
Hetzner WAN 65.21.16.177 : 443
  │  pfSense (vail) port-forward → DMZ
  ▼
NPM  (yllas 10.55.25.182)          ← TLS terminates here (Let's Encrypt cert, auto-renew)
  │  http → upstream
  ▼
web  (levi 10.55.20.10 : 8800)     ← Next.js; the ONLY published app port
  │  same-origin /api/* rewrite (web/next.config.mjs)
  ▼
api  (uvicorn : 8000, loopback)  ──►  postgres (pgvector)  +  redis
worker (arq)  ──────────────────────►  same DB/redis (background jobs)
```

Only `web:8800` is exposed on the app host. `api`, `postgres`, `redis` bind to
loopback; the browser reaches the API through the web container's same-origin
`/api/*` proxy.

---

## 2. Hosts

| Host | Role | Address | Specs | Notes |
|---|---|---|---|---|
| **levi** | App host (prod) | `10.55.20.10` (`levi.prod.dzsec.net`) | 4 vCPU / 16 GiB / 64 GiB | Debian 13 LXC · Docker 29.6.1 · Compose v5.3.1 · deploy dir `/home/administrator/ownchart` |
| **yllas** | Reverse proxy (DMZ) | `10.55.25.182` | 2 vCPU / 2 GiB | Nginx Proxy Manager — TLS + ingress. Admin UI on `:81` (internal only) |
| **ruka** | Staging (dev) | `10.55.26.25` (`ruka.dev.dzsec.net`) | 4 vCPU / 8 GiB / 64 GiB | Docker-ready, **idle** — provision a staging OwnChart here |

**SSH:** `administrator@levi.prod.dzsec.net` (from a machine on the DZsec
network). Passwordless sudo + Docker group.

> ⚠️ **Reachability:** as of standup, the Montana↔Lapland tunnel routes the DMZ
> (`yllas`) but **not the PROD/DEV subnets**, so `levi`/`ruka` are **not yet
> directly SSH-reachable from Montana** — `deploy.sh` (which SSHes from your
> workstation) will time out until `10.55.20.0/24`+`10.55.26.0/24` are added to
> the site-to-site VPN routing (infra task, tracked). Interim: deploy by running
> the compose commands **on levi directly** (reach it via the Proxmox host:
> `ssh root@lapland.os.dzsec.net` → `pct exec 201 -- …`), or from a host that can
> route to `10.55.20.10`.

---

## 3. The stack (compose project `ownchart`, 5 services)

| Service | Image | Port | Role |
|---|---|---|---|
| `web` | built from `web/Dockerfile` (Next.js) | **`0.0.0.0:8800`→3000** | Public UI; proxies `/api/*` to `api` |
| `api` | built from `api/Dockerfile` (FastAPI/uvicorn) | `127.0.0.1:8801`→8000 | REST API; **runs `alembic upgrade head` on start** |
| `worker` | same image as `api` | — | `arq` background worker (Claude vision extraction over PDFs, etc.) |
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:8802`→5432 | Primary DB (pgvector for embeddings) |
| `redis` | `redis:7-alpine` | `127.0.0.1:8803`→6379 | Queue / cache |

Compose files (`infra/`): `docker-compose.yml` (base, builds from source),
`docker-compose.images.yml` (additive override pinning published GHCR images),
`docker-compose.demo.yml` (demo mode — not used here).

---

## 4. Deploying a release

### 4a. Current method — source build via `infra/deploy.sh`

The repo ships `infra/deploy.sh`: it rsyncs the tree to the host and runs
`docker compose up -d --build`. To point it at **levi**, set `infra/deploy.env`
(gitignored):

```sh
OWNCHART_DEPLOY_USER=administrator
OWNCHART_DEPLOY_HOST=levi.prod.dzsec.net      # 10.55.20.10
OWNCHART_DEPLOY_DIR=/home/administrator/ownchart
OWNCHART_PUBLIC_BASE_URL=https://app.ownchart.me
OWNCHART_SMTP_PASSWORD=<mailcow app password>  # keep as-is
```

Provide the Anthropic key in `Working Docs/anthropic_dev_key.txt` (deploy.sh
reads it and refreshes the remote key each run). Then:

```sh
bash infra/deploy.sh              # rsync + build + up
bash infra/deploy.sh --no-build   # rsync only
bash infra/deploy.sh --logs       # tail remote logs
bash infra/deploy.sh --down       # stop stack (data preserved)
```

`deploy.sh` **preserves the remote `infra/.env` on subsequent runs** — the
Postgres password, session secret, and token DEK stay stable (see §5). It only
refreshes the Anthropic key + SMTP password.

### 4b. Recommended — prebuilt images (for CI/CD)

`docker-compose.images.yml` pins `ghcr.io/nickpdawson/ownchart-{api,web}`.

**How the images are produced.** `.github/workflows/release.yml` builds and
pushes on any `v*` tag, but only after a **fail-closed pipeline**:

1. **CI gate** — the workflow refuses to publish unless the CI workflow
   (`api-tests` + `web-build` + `dep-audit`) went **green on the exact tagged
   commit**. Tagging a commit that never passed CI on `dev`/`main` fails the
   release. (So the release flow is: land on `dev` → CI green → merge/ff to
   `main` → tag.)
2. **Multi-arch** — images are built for `linux/amd64` **and** `linux/arm64`.
3. **Supply-chain metadata** — each image carries an **SBOM** and **max
   provenance**, plus a signed **build-provenance attestation** pushed to the
   registry.
4. **Trivy scan** — the pushed image is scanned by digest; the moving `latest`
   tag is promoted **only after** it passes (fixable CRITICAL/HIGH fail the
   run). A scan failure leaves `latest` on the previous release.

**Deploying (tag or digest).** Two flows — see the header of
`infra/docker-compose.images.yml` for the copy-paste commands:

- **By tag** (simple): `export OWNCHART_IMAGE_TAG=v0.x.y` then `pull` + `up -d`
  with the two compose files.
- **By digest** (reproducible, recommended for prod): resolve the digest,
  optionally verify provenance, then pin `OWNCHART_API_REF` / `OWNCHART_WEB_REF`:
  ```sh
  # resolve the digest behind a tag
  docker buildx imagetools inspect ghcr.io/nickpdawson/ownchart-api:v0.x.y

  # verify the signed provenance attestation BEFORE trusting the digest
  gh attestation verify \
    oci://ghcr.io/nickpdawson/ownchart-api@sha256:<digest> \
    --repo nickpdawson/ownchart

  # pin + deploy (api + worker share the api image; refs default to the tag flow when unset)
  export OWNCHART_API_REF="@sha256:<api-digest>"
  export OWNCHART_WEB_REF="@sha256:<web-digest>"
  docker compose -f infra/docker-compose.yml -f infra/docker-compose.images.yml \
    --env-file infra/.env pull
  docker compose -f infra/docker-compose.yml -f infra/docker-compose.images.yml \
    --env-file infra/.env up -d
  ```

> **⚠️ OPERATOR TODO — GHCR package visibility (blocks this path).** The two
> GHCR packages are created **private** on first publish (pull returns
> `denied`), and this cannot be changed from the repo/CI — it's a
> **Packages settings** action on GitHub. Either:
>   1. Make `ownchart-api` and `ownchart-web` **public** (repo → Packages →
>      each package → *Package settings* → *Change visibility* → Public), **or**
>   2. Keep them private and `docker login ghcr.io` on **levi** with a
>      read-scoped (`read:packages`) PAT so `docker compose pull` can authenticate.
>
> Until one of these is done, only the source-build path (§4a) works.

Wiring `.github/workflows/release.yml` to a deploy step on levi would give you
push-to-tag deploys with no on-host build. **Recommended next step.**

---

## 5. Configuration & secrets — `infra/.env` on levi (mode 600)

| Variable | Source | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | **generated once** | Tied to the data volume — **never regenerate** or you lock out the DB |
| `SESSION_SECRET` | **generated once** | Regenerating invalidates all sessions |
| `OWNCHART_TOKEN_DEK` | **generated once** | Encrypts OAuth/provider tokens at rest — **losing it invalidates every connected provider**. Back this up. |
| `OWNCHART_AUTO_EXPORT_TOKEN` | generated once | Bearer for the iOS Health Auto Export push; rotating it means reconfiguring the app |
| `ANTHROPIC_API_KEY` | operator | Required for extraction/chat features |
| `OWNCHART_SMTP_PASSWORD` | operator | mailcow @ ownchart.me; non-secret SMTP host/from live in `infra/config.yaml` |
| `OWNCHART_EPIC_/ATHENA_/MODMED_/CERNER_/GOOGLE_CALENDAR_*` | operator | Set per connector as you enable each; unset → that connector returns 503 (fails closed) |
| `OWNCHART_ENV` | fixed `prod` | |
| `OWNCHART_PUBLIC_BASE_URL` | fixed `https://app.ownchart.me` | Used for OAuth redirects, email links |
| `ANTHROPIC_DEFAULT_MODEL` / `ANTHROPIC_VISION_MODEL` | config | Currently **`claude-opus-4-7`** |

> **Model note:** the current-latest Claude models are **Opus 4.8**
> (`claude-opus-4-8`), **Sonnet 5** (`claude-sonnet-5`), and Haiku 4.5. To move
> off `claude-opus-4-7`, just change `ANTHROPIC_DEFAULT_MODEL` /
> `ANTHROPIC_VISION_MODEL` in `.env` and redeploy — no code change. Consider
> `claude-opus-4-8` (quality) or `claude-sonnet-5` (cost/latency) for the beta.

`deploy.sh` handles generation + preservation of the "generated once" values.
**Do not hand-edit them.** Keep an offline backup of `OWNCHART_TOKEN_DEK`.

---

## 6. Database & migrations

- Postgres 16 + pgvector; data bind-mounted at `.../data/postgres`.
- `api` runs `alembic upgrade head` on every start (idempotent). Current head at standup: **`0053`**.
- App file data under `.../data/{evidence,renders,exports,backups,model_runs,directories}`.
- Migrations are **forward-only** — coordinate any schema-breaking change with the deploy; there's no auto-downgrade.
- **Backups are NOT yet configured** (see §8). This is the top gap for a prod-beta.

---

## 7. TLS / DNS / ingress

| Layer | Detail |
|---|---|
| DNS | `app.ownchart.me` → Cloudflare-**proxied** A record → `65.21.16.177` (Lapland WAN) |
| Edge TLS | Cloudflare edge cert (browser ↔ CF) |
| Origin TLS | Let's Encrypt cert **CN=app.ownchart.me** on NPM (yllas), issued via Cloudflare **dns-01**, **auto-renews**. CF↔origin is Full/strict. |
| Proxy host | NPM: `app.ownchart.me` → `http://10.55.20.10:8800`, SSL-forced, HTTP/2, **websockets enabled**, block-common-exploits on |
| Ingress FW | pfSense (vail) forwards WAN `:443` → `yllas:443` |
| NPM admin | `https://10.55.25.182:81` — credentials file on yllas at `/home/administrator/npm/npm-admin-credentials.txt` (root/admin only). Internal-only; not internet-exposed. |

Cert renewal uses a Cloudflare API token stored inside NPM (scoped Zone:DNS:Edit
+ Zone:Read for `ownchart.me`). If you rotate that token, update it on the NPM
cert or renewals will fail.

---

## 8. Operations (run on levi from `/home/administrator/ownchart` unless noted)

```sh
# health
curl -s localhost:8800/healthz                       # 200 expected
curl -sI https://app.ownchart.me/login               # public path, 200

# status / logs
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
docker compose -f infra/docker-compose.yml --env-file infra/.env logs -f --tail=200

# restart one service
docker compose -f infra/docker-compose.yml --env-file infra/.env restart api    # or web|worker

# redeploy (from your workstation)
bash infra/deploy.sh

# stop (keeps data)
bash infra/deploy.sh --down
```

**Rollback:** redeploy the previous git ref (source build) or previous
`OWNCHART_IMAGE_TAG` (images). Because migrations are forward-only, a rollback
that crosses a schema change needs a matching DB plan — don't roll back blindly
across a migration.

---

## 9. Open items / recommendations (ranked)

1. **Backups (do first).** Nightly `pg_dump` of the OwnChart DB + a copy of the
   `data/` file tree, pushed **off-host** (object storage or a Montana target —
   note the site's NFS to Montana is deliberately not used, it's too slow). A
   prod-beta should not run backup-less. Also snapshot/keep `OWNCHART_TOKEN_DEK`.
2. **CI deploy.** Publish the GHCR images publicly (or PAT) and add a deploy step
   on release tags → reproducible, build-free deploys to levi.
3. **Staging on ruka.** `ruka.dev.dzsec.net` (10.55.26.25) is provisioned and
   idle — run a staging OwnChart there to validate releases before levi.
4. **Model version.** Move off `claude-opus-4-7` to `claude-opus-4-8` /
   `claude-sonnet-5` as desired (§5) — `.env` only.
5. **Observability.** No app metrics / log shipping yet. Wire health + logs into
   monitoring (the site runs Zabbix; or an APM of your choice).
6. **Secret management.** Consider sourcing `.env` secrets from a manager
   (1Password/Vault) instead of on-host generation, to enable rotation.

---

## Appendix — quick facts

| Item | Value |
|---|---|
| Public URL | `https://app.ownchart.me` |
| App host (levi) | `10.55.20.10` · `levi.prod.dzsec.net` · deploy dir `/home/administrator/ownchart` |
| Proxy host (yllas) | `10.55.25.182` (NPM, TLS) |
| Staging host (ruka) | `10.55.26.25` · `ruka.dev.dzsec.net` |
| Public web port on levi | `8800` (→ container 3000) |
| Compose project | `ownchart` (`docker compose -f infra/docker-compose.yml --env-file infra/.env …`) |
| DB | Postgres 16 + pgvector; data at `data/postgres`; alembic head `0053` |
| Deploy tool | `infra/deploy.sh` (source build); `docker-compose.images.yml` (GHCR, currently private) |
| Ingress | Cloudflare (proxied) → WAN `65.21.16.177:443` → pfSense → NPM → `levi:8800` |
| TLS | LE cert on NPM via CF dns-01, auto-renew |
