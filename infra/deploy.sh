#!/usr/bin/env bash
# OwnChart deploy: rsync repo to Maverick + remote `docker compose up -d --build`.
#
# Usage:
#   bash infra/deploy.sh                  # full deploy
#   bash infra/deploy.sh --no-build       # rsync only
#   bash infra/deploy.sh --logs           # tail remote logs
#   bash infra/deploy.sh --down           # stop remote stack (does NOT delete data)
#
# This script never prints secrets and never commits .env to git.

set -euo pipefail

# Configure your deploy target via env vars or a local infra/deploy.env
# file (gitignored). Defaults below are placeholders — set them before
# running this script on a real host.
REMOTE_USER="${OWNCHART_DEPLOY_USER:-}"
REMOTE_HOST="${OWNCHART_DEPLOY_HOST:-}"
REMOTE_DIR="${OWNCHART_DEPLOY_DIR:-/home/${REMOTE_USER}/ownchart}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pull additional deploy config from a local, gitignored file if present.
# Useful spot for the host-specific values you don't want in env every time.
if [[ -f "${REPO_ROOT}/infra/deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/infra/deploy.env"
  REMOTE_USER="${REMOTE_USER:-${OWNCHART_DEPLOY_USER:-}}"
  REMOTE_HOST="${REMOTE_HOST:-${OWNCHART_DEPLOY_HOST:-}}"
  REMOTE_DIR="${REMOTE_DIR:-${OWNCHART_DEPLOY_DIR:-/home/${REMOTE_USER}/ownchart}}"
fi

if [[ -z "$REMOTE_USER" || -z "$REMOTE_HOST" ]]; then
  echo "❌ Set OWNCHART_DEPLOY_USER and OWNCHART_DEPLOY_HOST" >&2
  echo "   (export them, or drop them in infra/deploy.env)" >&2
  exit 1
fi

cd "$REPO_ROOT"

# --- preflight -------------------------------------------------------------
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${REMOTE_USER}@${REMOTE_HOST}" true 2>/dev/null; then
  echo "❌ Cannot SSH to ${REMOTE_USER}@${REMOTE_HOST}." >&2
  echo "   Check your SSH agent and that the host accepts your key." >&2
  exit 1
fi

KEY_FILE="${REPO_ROOT}/anthropic_dev_key.txt"
if [[ ! -s "$KEY_FILE" ]]; then
  echo "❌ Missing or empty: ${KEY_FILE}" >&2
  echo "   Drop the Anthropic dev key in that file (gitignored)." >&2
  exit 1
fi

case "${1:-}" in
  --logs)
    ssh "${REMOTE_USER}@${REMOTE_HOST}" \
      "cd ${REMOTE_DIR} && docker compose -f infra/docker-compose.yml --env-file infra/.env logs -f --tail=200"
    exit 0
    ;;
  --down)
    ssh "${REMOTE_USER}@${REMOTE_HOST}" \
      "cd ${REMOTE_DIR} && docker compose -f infra/docker-compose.yml --env-file infra/.env down"
    exit 0
    ;;
esac

# --- rsync ------------------------------------------------------------------
echo "→ rsync repo to ${REMOTE_HOST}:${REMOTE_DIR}"
# Pre-create data subdirs as administrator (matches container UID 1000) so
# the bind mount inherits admin ownership instead of root from the docker daemon.
ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}/data/evidence ${REMOTE_DIR}/data/renders ${REMOTE_DIR}/data/exports ${REMOTE_DIR}/data/backups ${REMOTE_DIR}/data/model_runs ${REMOTE_DIR}/data/directories"

rsync -az --delete \
  --exclude '.git/' \
  --exclude 'data/' \
  --exclude '**/node_modules/' \
  --exclude '**/.next/' \
  --exclude '**/.venv/' \
  --exclude '**/__pycache__/' \
  --exclude '**/.pytest_cache/' \
  --exclude '**/.mypy_cache/' \
  --exclude '**/.ruff_cache/' \
  --exclude 'anthropic_dev_key.txt' \
  --exclude 'infra/.env' \
  --exclude '*.log' \
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

# --- write infra/.env on the remote ----------------------------------------
# Generate fresh secrets the first time; on subsequent deploys, leave the
# existing infra/.env alone so the Postgres password stays consistent with
# the data volume.
echo "→ checking remote infra/.env"

ANTHROPIC_KEY_VAL="$(tr -d '[:space:]' < "$KEY_FILE")"

# Disable -e for this lookup so an absent file doesn't abort.
set +e
remote_env_exists=$(ssh "${REMOTE_USER}@${REMOTE_HOST}" "test -f ${REMOTE_DIR}/infra/.env && echo yes || echo no")
ssh_rc=$?
set -e
if [[ $ssh_rc -ne 0 ]]; then
  echo "❌ ssh lookup of remote .env failed (rc=$ssh_rc)" >&2
  exit 1
fi

if [[ "$remote_env_exists" == "yes" ]]; then
  echo "→ remote infra/.env exists — refreshing ANTHROPIC_API_KEY (preserving Postgres + session + token DEK)"
  ssh "${REMOTE_USER}@${REMOTE_HOST}" \
    "umask 077 && sed -i.bak -E 's|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_KEY_VAL}|' ${REMOTE_DIR}/infra/.env && rm -f ${REMOTE_DIR}/infra/.env.bak"

  # Backfill OWNCHART_TOKEN_DEK if absent (older deployments). Generate
  # ONLY when missing — losing the DEK invalidates encrypted tokens.
  has_dek=$(ssh "${REMOTE_USER}@${REMOTE_HOST}" "grep -c '^OWNCHART_TOKEN_DEK=' ${REMOTE_DIR}/infra/.env || true")
  if [[ "$has_dek" == "0" ]]; then
    echo "→ adding OWNCHART_TOKEN_DEK to existing .env (one-time)"
    NEW_DEK="$(LC_ALL=C dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 | tr -d '\n')"
    ssh "${REMOTE_USER}@${REMOTE_HOST}" \
      "umask 077 && printf 'OWNCHART_TOKEN_DEK=%s\n' '${NEW_DEK}' >> ${REMOTE_DIR}/infra/.env"
  fi

  # Backfill OWNCHART_AUTO_EXPORT_TOKEN if absent. This is the bearer
  # the Health Auto Export iOS app sends. Generate once and persist;
  # rotating it requires reconfiguring the iOS app, so we don't churn.
  has_aet=$(ssh "${REMOTE_USER}@${REMOTE_HOST}" "grep -c '^OWNCHART_AUTO_EXPORT_TOKEN=' ${REMOTE_DIR}/infra/.env || true")
  if [[ "$has_aet" == "0" ]]; then
    echo "→ adding OWNCHART_AUTO_EXPORT_TOKEN to existing .env (one-time)"
    NEW_AET="$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 48)"
    ssh "${REMOTE_USER}@${REMOTE_HOST}" \
      "umask 077 && printf 'OWNCHART_AUTO_EXPORT_TOKEN=%s\n' '${NEW_AET}' >> ${REMOTE_DIR}/infra/.env"
  fi
else
  echo "→ writing fresh remote infra/.env"
  PG_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 32)"
  SESSION_SECRET_VAL="$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 64)"
  TOKEN_DEK="$(LC_ALL=C dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 | tr -d '\n')"
  AUTO_EXPORT_TOKEN="$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 48)"
  ssh "${REMOTE_USER}@${REMOTE_HOST}" \
    "umask 077 && cat > ${REMOTE_DIR}/infra/.env" <<EOF
POSTGRES_USER=ownchart
POSTGRES_PASSWORD=${PG_PASSWORD}
POSTGRES_DB=ownchart

OWNCHART_ENV=prod
OWNCHART_DEBUG_PAYLOADS=false

SESSION_SECRET=${SESSION_SECRET_VAL}

# Encrypts OAuth tokens (provider_connections) at rest. Losing this
# invalidates every connected provider — preserve across deploys.
OWNCHART_TOKEN_DEK=${TOKEN_DEK}

OWNCHART_PUBLIC_BASE_URL=${OWNCHART_PUBLIC_BASE_URL:-http://localhost:8080}

# Bearer the Health Auto Export iOS app sends to /api/auto-export/push.
# Reconfigure the iOS app if you ever rotate this.
OWNCHART_AUTO_EXPORT_TOKEN=${AUTO_EXPORT_TOKEN}

ANTHROPIC_API_KEY=${ANTHROPIC_KEY_VAL}
ANTHROPIC_DEFAULT_MODEL=claude-opus-4-7
ANTHROPIC_VISION_MODEL=claude-opus-4-7
EOF
fi

# --- compose up -------------------------------------------------------------
if [[ "${1:-}" == "--no-build" ]]; then
  COMPOSE_CMD="docker compose -f infra/docker-compose.yml --env-file infra/.env up -d"
else
  COMPOSE_CMD="docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build"
fi

echo "→ remote: ${COMPOSE_CMD}"
ssh "${REMOTE_USER}@${REMOTE_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_CMD}"

echo "→ smoke check"
sleep 3
if curl --fail --silent --show-error --max-time 10 "http://${REMOTE_HOST}:8800/healthz" > /dev/null; then
  echo "✅ http://${REMOTE_HOST}:8800/healthz is up"
else
  echo "⚠️  /healthz did not respond yet — give it ~30s and check 'bash infra/deploy.sh --logs'"
fi
