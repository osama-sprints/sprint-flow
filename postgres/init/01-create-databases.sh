#!/bin/bash
# Runs ONCE, only when the postgres data volume is empty
# (docker-entrypoint-initdb.d). After changing it: docker compose down -v.
#
# One cluster, two databases: Mattermost's transactional data and ai-core's
# LangGraph checkpointer. No vector extensions live here — the vector workload
# is an external Qdrant instance, which is what keeps this cluster ordinary and
# keeps Mattermost's database free of anything exotic.
set -e

# The image already created "$POSTGRES_DB" (ai-core's). Mattermost gets its own.
# CREATE DATABASE cannot run inside a transaction block, which is why this is a
# .sh script — psql here runs without --single-transaction.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
    CREATE DATABASE ${MATTERMOST_DB};
EOSQL

echo "init: created database '${MATTERMOST_DB}' alongside '${POSTGRES_DB}'"
