#!/usr/bin/env bash
# Create the second database Mattermost expects.
#
# The compose file intentionally uses ONE Postgres cluster for both databases,
# but it only creates ai-core's database (POSTGRES_DB) by default. Mattermost's
# database (MATTERMOST_DB) must exist before Mattermost boots, otherwise the
# server exits immediately with:
#
#   pq: database "mattermost" does not exist (3D000)
#
# There is no init script for this in the repository today, which is why
# Mattermost is currently failing to start across every restart cycle.
#
# Run ONCE after a fresh clone / fresh Postgres data directory. Safe to re-run:
# CREATE DATABASE IF NOT EXISTS is not standard SQL, so we guard with a lookup.

set -euo pipefail

DB="${MATTERMOST_DB:-mattermost}"
CLUSTER_USER="${POSTGRES_USER:-sprintflow}"
CLUSTER_PASS="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"

DB_OK=""
DB_OK=$(psql -U "$CLUSTER_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '$DB';" | tr -d '[:space:]')

if [ "$DB_OK" = "1" ]; then
  echo "database '$DB' already exists"
  exit 0
fi

echo "creating database '$DB' on cluster"
PGPASSWORD="$CLUSTER_PASS" psql -U "$CLUSTER_USER" -d postgres -c "CREATE DATABASE \"$DB\";"
echo "done"
