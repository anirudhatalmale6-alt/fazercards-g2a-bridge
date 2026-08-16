#!/usr/bin/env bash
# One-shot deploy / redeploy.
#
#   ./ops/deploy.sh            build, start, wait for health, run doctor
#   ./ops/deploy.sh --setup    the above, plus the one-time catalogue + mapping run
#
# Safe to re-run: everything it does is idempotent.

set -euo pipefail
cd "$(dirname "$0")/.."

RUN_SETUP=0
[[ "${1:-}" == "--setup" ]] && RUN_SETUP=1

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing. Start from the template:" >&2
  echo "  cp .env.example .env" >&2
  echo "  python -m app.cli keygen   # paste into ENCRYPTION_KEY" >&2
  exit 1
fi

# Fail early and clearly rather than letting a container start half-configured.
missing=()
for var in POSTGRES_PASSWORD FAZERCARDS_API_KEY G2A_CLIENT_ID G2A_CLIENT_SECRET ENCRYPTION_KEY ADMIN_API_TOKEN; do
  value=$(grep -E "^${var}=" .env | head -1 | cut -d= -f2- || true)
  [[ -z "${value}" ]] && missing+=("${var}")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: these are empty in .env: ${missing[*]}" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: docker compose is not installed." >&2
  exit 1
fi

echo "==> Building"
$DC build

echo "==> Starting"
$DC up -d

echo "==> Waiting for the API to become healthy"
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "    healthy after ${attempt}s"
    break
  fi
  if [[ ${attempt} -eq 30 ]]; then
    echo "ERROR: the API did not become healthy. Recent logs:" >&2
    $DC logs --tail=50 api >&2
    exit 1
  fi
  sleep 1
done

echo "==> Checking credentials and reaching both APIs"
$DC exec -T api python -m app.cli doctor

if [[ ${RUN_SETUP} -eq 1 ]]; then
  echo "==> Mirroring the store catalogue (this is the slow one: 20 products per page)"
  $DC exec -T api python -m app.cli sync store-catalog

  echo "==> Pulling the supplier catalogue"
  $DC exec -T api python -m app.cli sync supplier

  echo "==> Matching"
  $DC exec -T api python -m app.cli map run

  echo "==> Adopting offers that already exist on the store"
  $DC exec -T api python -m app.cli sync adopt

  echo
  echo "Setup done. Nothing has been pushed to the store yet."
  echo "Review what is unsure, then do a dry run before going live:"
  echo "  $DC exec api python -m app.cli map pending"
  echo "  $DC exec api python -m app.cli sync offers --dry-run"
fi

echo
echo "Done. The worker is running. Follow it with:"
echo "  $DC logs -f worker"
