#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-}"
if [[ -n "$MODE" && "$MODE" != "--live" ]]; then
  echo "Usage: $0 [--live]"
  exit 2
fi

./scripts/verify_data_model.sh
./scripts/verify_orchestration.sh "$MODE"
./scripts/verify_authorisation.sh "$MODE"
./scripts/verify_ceremony.sh
./scripts/verify_onboarding_feature.sh "$MODE"
./scripts/verify_platform.sh "$MODE"

echo
echo "========================================================================"
if [[ "$MODE" == "--live" ]]; then
  echo "ALL FEATURE AND LIVE INTEGRATION CHECKS PASSED"
else
  echo "ALL FAST FEATURE CHECKS PASSED"
  echo "Run './scripts/verify_all_features.sh --live' for full integration coverage."
fi
echo "========================================================================"
