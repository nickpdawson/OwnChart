#!/usr/bin/env bash
# OwnChart demo deploy — rsync repo to the demo host + remote
# `docker compose -f infra/docker-compose.demo.yml ... up -d --build`.
#
# Usage:
#   bash infra/deploy-demo.sh             # full deploy
#   bash infra/deploy-demo.sh --logs      # tail remote logs
#   bash infra/deploy-demo.sh --down      # stop remote stack
#
# Requires: infra/.env.demo and infra/deploy.env on the LOCAL host
# (both gitignored). See infra/.env.demo.example and
# infra/deploy.env.example for the shape.

set -euo pipefail

# Reuse the same deploy.env layout as production. The demo deploy
# uses the OWNCHART_DEMO_HOST var (falls back to OWNCHART_DEPLOY_HOST
# so a single-host setup doesn't need a separate file).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${REPO_ROOT}/infra/deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/infra/deploy.env"
fi

REMOTE_USER="${OWNCHART_DEMO_USER:-${OWNCHART_DEPLOY_USER:-}}"
REMOTE_HOST="${OWNCHART_DEMO_HOST:-${OWNCHART_DEPLOY_HOST:-}}"
REMOTE_DIR="${OWNCHART_DEMO_DIR:-${OWNCHART_DEPLOY_DIR:-/home/${REMOTE_USER}/ownchart-demo}}"
PROJECT_NAME="ownchart-demo"
COMPOSE_FILE="infra/docker-compose.demo.yml"
ENV_FILE="infra/.env.demo"

if [[ -z "$REMOTE_USER" || -z "$REMOTE_HOST" ]]; then
  echo "Set OWNCHART_DEMO_HOST + OWNCHART_DEMO_USER (or the OWNCHART_DEPLOY_* equivalents)." >&2
  echo "Put them in infra/deploy.env or export before running." >&2
  exit 1
fi

cd "$REPO_ROOT"

if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${REMOTE_USER}@${REMOTE_HOST}" true 2>/dev/null; then
  echo "Cannot SSH to ${REMOTE_USER}@${REMOTE_HOST}." >&2
  exit 1
fi

DC_REMOTE="cd ${REMOTE_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE} --project-name ${PROJECT_NAME}"

case "${1:-}" in
  --logs)
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "${DC_REMOTE} logs -f --tail=200"
    exit 0
    ;;
  --down)
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "${DC_REMOTE} down"
    exit 0
    ;;
esac

if [[ ! -f "infra/.env.demo" ]]; then
  echo "Missing local infra/.env.demo (gitignored)." >&2
  echo "Copy infra/.env.demo.example, fill in real values, and re-run." >&2
  exit 1
fi

echo "rsync repo to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '__pycache__' \
  --exclude '.next' \
  --exclude 'data' \
  --exclude 'data-demo' \
  --exclude '.DS_Store' \
  --exclude 'anthropic_dev_key.txt' \
  --exclude 'infra/.env' \
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

# Push the demo env file separately (rsync excluded it from the
# generic --exclude above to avoid leaking prod secrets if both
# files happen to live in the same tree).
rsync -az infra/.env.demo "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/infra/.env.demo"

# Optional: ship the demo bundle if present locally.
if [[ -f "infra/demo_data/sample_patient.json" ]]; then
  rsync -az infra/demo_data/ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/infra/demo_data/"
fi

echo "build + up on remote"
ssh "${REMOTE_USER}@${REMOTE_HOST}" "${DC_REMOTE} up -d --build"

echo "smoke check (host:9988)"
ssh "${REMOTE_USER}@${REMOTE_HOST}" "curl -fsS -o /dev/null -w 'container 9988: %{http_code}\n' http://localhost:9988/healthz || true"
echo "TLS check (demo.ownchart.me) — verify NPM forwarder + DNS once live."
