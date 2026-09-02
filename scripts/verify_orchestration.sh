#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Feature: Multi-agent orchestration and delegation"

run_ai_python -m app.core.langgraph.routing_rules

if live_requested "${1:-}"; then
  run_host_python scripts/verify_routing.py
  run_host_python scripts/verify_threading.py
  run_host_python scripts/verify_isolation.py
else
  echo "INFO: live transport, threading, and isolation checks skipped; use --live to include them."
fi

echo "PASS: orchestration verification completed."
