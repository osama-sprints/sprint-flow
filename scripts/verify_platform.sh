#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Cross-feature platform integration"

if ! live_requested "${1:-}"; then
  echo "INFO: platform checks call Mattermost, the LLM, and Qdrant. Re-run with --live."
  exit 0
fi

./scripts/smoke_test.sh
run_host_python scripts/verify_memory.py

echo "PASS: end-to-end bot pipeline and long-term memory verification completed."
