#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Feature: Authorisation and back-office administration"

run_core_pytest tests/test_authorisation_sprint1.py -v

if live_requested "${1:-}"; then
  run_host_python scripts/verify_admin_mapping.py
  run_host_python scripts/verify_admin_agent.py
else
  echo "INFO: live identity mapping and Mattermost admin checks skipped; use --live to include them."
fi

echo "PASS: authorisation verification completed."
