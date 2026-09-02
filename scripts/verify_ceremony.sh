#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Feature: Ceremony scheduling through conversation"

run_core_pytest tests/test_ceremony_scheduler.py -v

echo "PASS: ceremony validation, permissions, cohort lookup, conflicts, scheduling, amendment, and reads passed."
