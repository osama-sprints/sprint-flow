#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_verify_common.sh"

verify_heading "Feature: Data model and migrations"
require_service postgres

run_ai_python - < ai-core/scripts/verify_schema.py
docker compose exec -T ai-core /app/.venv/bin/alembic current

echo "PASS: data-model schema is present and Alembic reported the current revision."
