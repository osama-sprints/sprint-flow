#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Feature: Proactive role-aware onboarding"

run_ai_python -m py_compile /app/app/services/onboarding.py
ai-core/.venv/bin/ruff check --select F821 ai-core/app/services/onboarding.py

if live_requested "${1:-}"; then
  run_host_python scripts/verify_onboarding.py
  run_host_python scripts/verify_task5_onboarding.py
else
  echo "INFO: onboarding side-effect and restart checks skipped; use --live to include them."
fi

echo "PASS: onboarding verification completed."
