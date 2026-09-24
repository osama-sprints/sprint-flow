#!/usr/bin/env bash
# Bootstrap a running Mattermost for SprintFlow, non-interactively and idempotently.
#
# ORDER MATTERS. The bot is promoted to system_admin BEFORE team creation is
# locked down, because the lockdown works by removing `create_team` from the
# `system_user` role — which the bot also holds. Reverse these two and the
# Admin agent loses the permission it needs, silently.
#
# Usage:  ./scripts/bootstrap_mattermost.sh
set -euo pipefail

# Dynamically find working Python binary across Windows/Linux/Git Bash environments
PYTHON_BIN=""
for cmd in python3 python py; do
  if command -v "$cmd" >/dev/null 2>&1; then
    if "$cmd" -c "import sys" >/dev/null 2>&1; then
      PYTHON_BIN="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: No working Python executable found (python3, python, or py)."
  echo "If on Windows, ensure Python is added to your PATH or app execution aliases are disabled."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=""
for candidate in \
  "$SCRIPT_DIR/.." \
  "$SCRIPT_DIR" \
  "${PWD}" \
  "/app"; do
  if [[ -f "$candidate/.env" ]]; then
    ROOT="$(cd "$candidate" && pwd)"
    break
  fi
done
ENV_FILE="$ROOT/.env"

[[ -n "$ROOT" ]] || { echo "ERROR: .env not found. Copy .env.example to .env first."; exit 1; }
set -a; source "$ENV_FILE"; set +a

MM_PORT="${MATTERMOST_HOST_PORT:-8065}"
MM_HOST="${MATTERMOST_HOST:-localhost}"
MM_API="http://${MM_HOST}:${MM_PORT}/api/v4"
MMCTL="/mattermost/bin/mmctl"
BOT_USERNAME="${MATTERMOST_BOT_USERNAME:-sprintflow-assistant}"
TEAM_NAME="${MM_TEAM_NAME:-sprints-community}"
TEAM_DISPLAY="${MM_TEAM_DISPLAY_NAME:-Sprints Community}"
BOT_CHANNEL="${MM_BOT_CHANNEL:-town-square}"
# Space-separated, matching Mattermost's own slice encoding for this setting.
DEFAULT_CHANNELS="${MM_DEFAULT_CHANNELS:-announcements engineering helpdesk watercooler}"
TRIGGER_WORDS="${MATTERMOST_TRIGGER_WORDS:-@${BOT_USERNAME},!ask}"
export BOT_USERNAME TEAM_NAME TEAM_DISPLAY TRIGGER_WORDS

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[0;33m!\033[0m %s\n' "$1"; }

# jget <json> <python expr over `d`> — "" when absent.
jget() { "$PYTHON_BIN" -c "
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
if isinstance(d, dict) and 'status_code' in d and 'message' in d:
    print(''); sys.exit()
try: print(eval(sys.argv[2]) or '')
except Exception: print('')
" "$1" "$2"; }

mmctl() { MSYS_NO_PATHCONV=1 docker compose exec -T mattermost "$MMCTL" --local "$@"; }

# ------------------------------------------------------------------ wait ----
say "Waiting for Mattermost on port ${MM_PORT}"
for i in $(seq 1 60); do
  curl -sf "${MM_API}/system/ping" >/dev/null 2>&1 && { ok "responding"; break; }
  [[ $i -eq 60 ]] && { echo "ERROR: Mattermost did not come up. Try: docker compose logs mattermost"; exit 1; }
  sleep 3
done

# ----------------------------------------------------------------- admin ----
say "Creating system admin '${MM_ADMIN_USERNAME}'"
mmctl user create --email "$MM_ADMIN_EMAIL" --username "$MM_ADMIN_USERNAME" \
    --password "$MM_ADMIN_PASSWORD" --system-admin --email-verified 2>&1 | tail -1 || warn "may already exist"

say "Authenticating over the REST API"
LOGIN_PAYLOAD="{\"login_id\":\"${MM_ADMIN_USERNAME}\",\"password\":\"${MM_ADMIN_PASSWORD}\"}"
ADMIN_TOKEN=$(curl -sS -i -H 'Content-Type: application/json' -d "$LOGIN_PAYLOAD" "$MM_API/users/login" | grep -i '^token:' | tail -1 | tr -d '\r' | awk '{print $2}')

[[ -n "$ADMIN_TOKEN" ]] || { echo "ERROR: admin login failed"; exit 1; }
ok "authenticated"

AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json')
api() {
  local m="$1" p="$2" body="${3:-}"
  if [[ -n "$body" ]]; then curl -sS -X "$m" "${AUTH[@]}" -d "$body" "$MM_API$p"
  else curl -sS -X "$m" "${AUTH[@]}" "$MM_API$p"; fi
}

# ------------------------------------------------------------------- bot ----
say "Creating bot '@${BOT_USERNAME}'"
BOT_USER_ID=$(jget "$(api GET "/users/username/$BOT_USERNAME")" "d['id']")
if [[ -z "$BOT_USER_ID" ]]; then
  BOT_PAYLOAD="{\"username\":\"${BOT_USERNAME}\",\"display_name\":\"SprintFlow Assistant\",\"description\":\"AI colleague for the SprintFlow workspace\"}"
  BOT=$(api POST "/bots" "$BOT_PAYLOAD")
  BOT_USER_ID=$(jget "$BOT" "d['user_id']")
fi
[[ -n "$BOT_USER_ID" ]] || { echo "ERROR: could not create bot"; exit 1; }
export BOT_USER_ID
ok "bot user id ${BOT_USER_ID}"

# ------------------------------------------------- GATE: promote, then lock --
say "Promoting the bot to system_admin  [must precede the lockdown]"
mmctl roles system-admin "$BOT_USERNAME" 2>&1 | tail -1
BOT_ROLES=$(jget "$(api GET "/users/$BOT_USER_ID")" "d['roles']")
case " $BOT_ROLES " in
  *" system_admin "*) ok "bot roles: ${BOT_ROLES}" ;;
  *) echo "ERROR: bot was not promoted (roles: ${BOT_ROLES}). Aborting before lockdown."; exit 1 ;;
esac

say "Locking down team creation for regular users"
mmctl permissions remove system_user create_team 2>&1 | tail -1 || warn "already removed"
if mmctl permissions role show system_user 2>&1 | tr ' ' '\n' | grep -qx "create_team"; then
  warn "create_team still present on system_user — lockdown did NOT apply"
else
  ok "regular users can no longer create teams"
fi

# ------------------------------------------------------------------ team ----
say "Creating team '${TEAM_DISPLAY}' (${TEAM_NAME})"
TEAM_ID=$(jget "$(api GET "/teams/name/$TEAM_NAME")" "d['id']")
if [[ -z "$TEAM_ID" ]]; then
  TEAM_PAYLOAD="{\"name\":\"${TEAM_NAME}\",\"display_name\":\"${TEAM_DISPLAY}\",\"type\":\"O\"}"
  TEAM=$(api POST "/teams" "$TEAM_PAYLOAD")
  TEAM_ID=$(jget "$TEAM" "d['id']")
fi
[[ -n "$TEAM_ID" ]] || { echo "ERROR: could not create team"; exit 1; }
export TEAM_ID
ok "team id ${TEAM_ID}"

ADMIN_ID=$(jget "$(api GET "/users/username/$MM_ADMIN_USERNAME")" "d['id']")
api POST "/teams/$TEAM_ID/members" "{\"team_id\":\"$TEAM_ID\",\"user_id\":\"$ADMIN_ID\"}" >/dev/null || true
api POST "/teams/$TEAM_ID/members" "{\"team_id\":\"$TEAM_ID\",\"user_id\":\"$BOT_USER_ID\"}" >/dev/null || true
ok "admin and bot added to team"

# -------------------------------------------------------------- channels ----
channel_display() {
  case "$1" in
    town-square) echo "General" ;;
    *) "$PYTHON_BIN" -c "import sys;print(sys.argv[1].replace('-',' ').title())" "$1" ;;
  esac
}

channel_header() {
  case "$1" in
    town-square)    echo "General company-wide discussion and workspace chatter." ;;
    announcements)  echo "Company-wide announcements and official updates." ;;
    engineering)    echo "Architecture, code review and release coordination." ;;
    helpdesk)       echo "Ask for help with tooling, access, or anything blocking you." ;;
    watercooler)    echo "Off-topic chat. Non-work welcome." ;;
    *) echo "" ;;
  esac
}

describe_channel() {
  local ch_id="$1" ch_name="$2"
  local header body
  export CH_DISPLAY CH_HEADER
  CH_DISPLAY="$(channel_display "$ch_name")"
  CH_HEADER="$(channel_header "$ch_name")"
  [[ -z "$CH_HEADER" ]] && return 0
  body="{\"display_name\":\"${CH_DISPLAY}\",\"header\":\"${CH_HEADER}\",\"purpose\":\"${CH_HEADER}\"}"
  api PUT "/channels/$ch_id/patch" "$body" >/dev/null
}

say "Creating default channels: ${DEFAULT_CHANNELS}"
for ch in $DEFAULT_CHANNELS; do
  CH_ID=$(jget "$(api GET "/teams/$TEAM_ID/channels/name/$ch")" "d['id']")
  if [[ -z "$CH_ID" ]]; then
    CH_DISP="$(channel_display "$ch")"
    BODY="{\"team_id\":\"${TEAM_ID}\",\"name\":\"${ch}\",\"display_name\":\"${CH_DISP}\",\"type\":\"O\"}"
    CH_ID=$(jget "$(api POST "/channels" "$BODY")" "d['id']")
  fi
  [[ -z "$CH_ID" ]] && { warn "could not create #${ch}"; continue; }
  describe_channel "$CH_ID" "$ch"
  api POST "/channels/$CH_ID/members" "{\"user_id\":\"$BOT_USER_ID\"}" >/dev/null 2>&1 || true
  ok "#${ch} ready, described, bot joined"
done

CHANNEL_ID=$(jget "$(api GET "/teams/$TEAM_ID/channels/name/$BOT_CHANNEL")" "d['id']")
[[ -n "$CHANNEL_ID" ]] || { echo "ERROR: channel '$BOT_CHANNEL' not found"; exit 1; }
export CHANNEL_ID
describe_channel "$CHANNEL_ID" "$BOT_CHANNEL"
api POST "/channels/$CHANNEL_ID/members" "{\"user_id\":\"$BOT_USER_ID\"}" >/dev/null 2>&1 || true
ok "#${BOT_CHANNEL} renamed to '$(channel_display "$BOT_CHANNEL")', bot joined"

# ------------------------------------------------------------ bot token -----
say "Issuing a Personal Access Token for the bot"
TOKEN_JSON=$(api POST "/users/$BOT_USER_ID/tokens" '{"description":"SprintFlow ai-core"}')
BOT_TOKEN=$(jget "$TOKEN_JSON" "d['token']")
[[ -n "$BOT_TOKEN" ]] || { echo "ERROR: could not create bot token: $TOKEN_JSON"; exit 1; }
ok "token issued"

# ------------------------------------------------------ outgoing webhook ----
say "Creating the outgoing webhook -> http://ai-core:8000"

EXISTING_HOOKS=$(api GET "/hooks/outgoing?team_id=${TEAM_ID}&channel_id=${CHANNEL_ID}")

WEBHOOK_TOKEN=$("$PYTHON_BIN" -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    if isinstance(data, list):
        for h in data:
            if 'http://ai-core:8000/api/v1/mattermost/webhook' in h.get('callback_urls', []):
                print(h.get('token', ''))
                sys.exit(0)
except Exception:
    pass
print('')
" "$EXISTING_HOOKS")

if [[ -z "$WEBHOOK_TOKEN" ]]; then
  HOOK_PAYLOAD=$("$PYTHON_BIN" -c '
import json, os
print(json.dumps({
    "team_id": os.environ["TEAM_ID"],
    "channel_id": os.environ["CHANNEL_ID"],
    "display_name": "SprintFlow Assistant",
    "description": "Routes @mentions to the ai-core LangGraph agent",
    "trigger_words": [w.strip() for w in os.environ["TRIGGER_WORDS"].split(",") if w.strip()],
    "trigger_when": 0,
    "callback_urls": ["http://ai-core:8000/api/v1/mattermost/webhook"],
    "content_type": "application/json",
}))')
  HOOK_JSON=$(api POST "/hooks/outgoing" "$HOOK_PAYLOAD")
  WEBHOOK_TOKEN=$(jget "$HOOK_JSON" "d['token']")
fi

[[ -n "$WEBHOOK_TOKEN" ]] || { echo "ERROR: could not create or find outgoing webhook"; exit 1; }
ok "webhook ready (token: ${WEBHOOK_TOKEN:0:6}...)"

# ---------------------------------------------------------------- brand -----
say "Applying branding"
if [[ -x ./scripts/prepare_branding.sh ]]; then
  ./scripts/prepare_branding.sh >/dev/null 2>&1 || warn "branding rasterisation failed"
fi
if [[ -f branding/generated/login-logo.png ]]; then
  if curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -F "image=@branding/generated/login-logo.png" "$MM_API/brand/image" >/dev/null; then
    ok "login logo uploaded"
  else
    warn "login logo upload failed"
  fi
fi
if [[ -f branding/generated/team-icon.png ]]; then
  if curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -F "image=@branding/generated/team-icon.png" "$MM_API/teams/$TEAM_ID/image" >/dev/null; then
    ok "team icon uploaded"
  else
    warn "team icon upload failed"
  fi
fi

# ------------------------------------------------------------ write .env ----
# ------------------------------------------------------------ write .env ----
say "Writing tokens into .env"

# Resolve path with forward slashes to avoid Windows backslash escape issues
ENV_WIN_PATH="$(cygpath -m "$ENV_FILE" 2>/dev/null || echo "$ENV_FILE")"

"$PYTHON_BIN" - "$ENV_WIN_PATH" "$BOT_TOKEN" "$WEBHOOK_TOKEN" <<'PY'
import re, sys

path = sys.argv[1]
bot_token = sys.argv[2]
webhook_token = sys.argv[3]

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

updates = {
    "MATTERMOST_BOT_TOKEN": bot_token,
    "MATTERMOST_OUTGOING_WEBHOOK_TOKEN": webhook_token,
}

for key, val in updates.items():
    if re.search(rf"(?m)^{key}=.*$", content):
        content = re.sub(rf"(?m)^{key}=.*$", f"{key}={val}", content)
    else:
        content += f"\n{key}={val}\n"

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("    updated MATTERMOST_BOT_TOKEN and MATTERMOST_OUTGOING_WEBHOOK_TOKEN")
PY

say "Restarting ai-core so it picks up the new tokens"
docker compose up -d --force-recreate --no-deps ai-core >/dev/null
ok "ai-core restarted"

# Replace line 303/304 with this:
cat <<'SUMMARY'

──────────────────────────────────────────────────────────────
 SprintFlow is ready.

    Mattermost   http://localhost:8065
    login        admin / <your_admin_password>
    team         Sprints Community
    ai-core      internal only (no published port)

 Try it:    @sprintflow-assistant hello
 Verify:    ./scripts/smoke_test.sh && python3 scripts/verify_routing.py
──────────────────────────────────────────────────────────────
SUMMARY