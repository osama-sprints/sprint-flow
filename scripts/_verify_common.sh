#!/usr/bin/env bash
set -Eeuo pipefail

VERIFY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$VERIFY_ROOT"

if [[ ! -f .env ]]; then
  echo "FAIL: $VERIFY_ROOT/.env is required. Copy .env.example and configure it first."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

verify_heading() {
  echo
  echo "========================================================================"
  echo "$1"
  echo "========================================================================"
}

require_service() {
  local service="$1"
  if ! docker compose ps --status running --services | grep -Fxq "$service"; then
    echo "FAIL: compose service '$service' is not running. Start the stack first."
    exit 1
  fi
}

run_ai_python() {
  require_service ai-core
  docker compose exec -T ai-core /app/.venv/bin/python "$@"
}

run_host_python() {
  python3 "$@"
}

run_core_pytest() {
  if [[ ! -x ai-core/.venv/bin/python ]]; then
    echo "FAIL: ai-core/.venv is missing. Run 'make -C ai-core install' first."
    exit 1
  fi
  (
    cd ai-core
    PYTHONPATH=. .venv/bin/python -m pytest "$@"
  )
}

live_requested() {
  [[ "${1:-}" == "--live" ]]
}
