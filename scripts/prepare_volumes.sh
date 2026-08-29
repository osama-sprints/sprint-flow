#!/usr/bin/env bash
# Create the bind-mounted directories and give them the uids the containers run
# as. Host ownership matters because bind mounts keep host permissions:
#
#   mattermost/volumes -> 2000:2000  (the Mattermost image runs as uid 2000 and
#                                     cannot write its config.json otherwise)
#   ai-core/logs       -> 1000:1000  (the ai-core image runs as appuser, uid 1000,
#                                     and structlog opens a JSONL file on import)
#
# chown is done inside a throwaway root container, so no host sudo is needed.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p mattermost/volumes/app/mattermost/{config,data,logs,plugins,client/plugins,bleve-indexes}
mkdir -p ai-core/logs

docker run --rm \
  -v "$PWD/mattermost/volumes:/mm" \
  -v "$PWD/ai-core/logs:/logs" \
  alpine:3.20 sh -c 'chown -R 2000:2000 /mm && chown -R 1000:1000 /logs'

echo "volumes prepared: mattermost/volumes (2000:2000), ai-core/logs (1000:1000)"
