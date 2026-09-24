#!/usr/bin/env bash
# ai-core/scripts/docker-entrypoint.sh

set -e

# Print initial environment values (before loading .env)
echo "Starting with these environment variables:"
echo "APP_ENV: ${APP_ENV:-development}"
echo "Initial Database Host: $( [[ -n ${POSTGRES_HOST:-${DB_HOST:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database Port: $( [[ -n ${POSTGRES_PORT:-${DB_PORT:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database Name: $( [[ -n ${POSTGRES_DB:-${DB_NAME:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database User: $( [[ -n ${POSTGRES_USER:-${DB_USER:-}} ]] && echo 'set' || echo 'Not set' )"

# Load environment variables from the appropriate .env file
load_env_file() {
    local env_file="$1"
    echo "Loading environment from $env_file"
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and blank lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue

        # Only accept valid KEY=VALUE lines (key must start with letter or underscore)
        if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            key="${line%%=*}"
            if [[ -z "${!key}" ]]; then
                # Use a safer export that handles values with spaces/quotes better
                export "$line"
            else
                echo "Keeping existing value for $key"
            fi
        else
            echo "WARNING: Skipping invalid env line → $line"
        fi
    done < "$env_file"
}

if [ -f ".env.${APP_ENV}" ]; then
    load_env_file ".env.${APP_ENV}"
elif [ -f ".env" ]; then
    load_env_file ".env"
else
    echo "Warning: No .env file found. Using system environment variables."
fi

# Check required sensitive environment variables
required_vars=("JWT_SECRET_KEY" "OPENAI_API_KEY")
missing_vars=()

for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        missing_vars+=("$var")
    fi
done

if [[ ${#missing_vars[@]} -gt 0 ]]; then
    echo "ERROR: The following required environment variables are missing:"
    for var in "${missing_vars[@]}"; do
        echo "  - $var"
    done
    echo "Please provide these variables through environment or .env files."
    exit 1
fi

# Print final environment info
echo -e "\nFinal environment configuration:"
echo "Environment: ${APP_ENV:-development}"

echo "Database Host: $( [[ -n ${POSTGRES_HOST:-${DB_HOST:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database Port: $( [[ -n ${POSTGRES_PORT:-${DB_PORT:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database Name: $( [[ -n ${POSTGRES_DB:-${DB_NAME:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database User: $( [[ -n ${POSTGRES_USER:-${DB_USER:-}} ]] && echo 'set' || echo 'Not set' )"

echo "LLM Model: ${DEFAULT_LLM_MODEL:-Not set}"
echo "Debug Mode: ${DEBUG:-false}"

if [[ "${AI_CORE_MIGRATE_ON_START:-true}" == "true" ]]; then
    echo "Applying database migrations: alembic upgrade head"
    uv run alembic upgrade head
    echo "Database schema is at head"
else
    echo "Skipping database migrations (AI_CORE_MIGRATE_ON_START=${AI_CORE_MIGRATE_ON_START})"
fi

# Automatically ingest sample policies on startup if enabled
if [[ "${INGEST_POLICIES_ON_START:-true}" == "true" ]]; then
    echo "Ingesting sample policy documents into vector store..."
    uv run python -m app.services.document_ingestion.pipeline || echo "Policy ingestion encountered an issue."
fi

# Execute the CMD
exec "$@"