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

cd "$(dirname "$0")/.."
ROOT="$PWD"
ENV_FILE="$ROOT/.env"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: .env not found. Copy .env.example to .env first."; exit 1; }
set -a; source "$ENV_FILE"; set +a

MM_PORT="${MATTERMOST_HOST_PORT:-8065}"
MM_API="http://localhost:${MM_PORT}/api/v4"
MMCTL="/mattermost/bin/mmctl"
BOT_USERNAME="${MATTERMOST_BOT_USERNAME:-sprintflow-assistant}"
TEAM_NAME="${MM_TEAM_NAME:-sprints-community}"
TEAM_DISPLAY="${MM_TEAM_DISPLAY_NAME:-Sprints Community}"
BOT_CHANNEL="${MM_BOT_CHANNEL:-town-square}"
# Space-separated, matching Mattermost's own slice encoding for this setting.
DEFAULT_CHANNELS="${MM_DEFAULT_CHANNELS:-qa support}"
TRIGGER_WORDS="${MATTERMOST_TRIGGER_WORDS:-@${BOT_USERNAME},!ask}"
export BOT_USERNAME TEAM_NAME TEAM_DISPLAY TRIGGER_WORDS

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[0;33m!\033[0m %s\n' "$1"; }

# jget <json> <python expr over `d`> — "" when absent.
# Mattermost reports errors as a JSON OBJECT that also carries an "id" field, so
# a naive d['id'] would return the error code as if it were a valid id.
jget() { python3 -c "
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
if isinstance(d, dict) and 'status_code' in d and 'message' in d:
    print(''); sys.exit()
try: print(eval(sys.argv[2]) or '')
except Exception: print('')
" "$1" "$2"; }

mmctl() { docker compose exec -T mattermost "$MMCTL" --local "$@"; }

# ------------------------------------------------------------------ wait ----
say "Waiting for Mattermost on port ${MM_PORT}"
for i in $(seq 1 60); do
  curl -sf "http://localhost:${MM_PORT}/api/v4/system/ping" >/dev/null 2>&1 && { ok "responding"; break; }
  [[ $i -eq 60 ]] && { echo "ERROR: Mattermost did not come up. Try: docker compose logs mattermost"; exit 1; }
  sleep 3
done

# ----------------------------------------------------------------- admin ----
say "Creating system admin '${MM_ADMIN_USERNAME}'"
mmctl user create --email "$MM_ADMIN_EMAIL" --username "$MM_ADMIN_USERNAME" \
    --password "$MM_ADMIN_PASSWORD" --system-admin --email-verified 2>&1 | tail -1 || warn "may already exist"

say "Authenticating over the REST API"
H=$(mktemp)
curl -sS -D "$H" -o /dev/null -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,os;print(json.dumps({"login_id":os.environ["MM_ADMIN_USERNAME"],"password":os.environ["MM_ADMIN_PASSWORD"]}))')" \
  "$MM_API/users/login"
ADMIN_TOKEN=$(grep -i '^token:' "$H" | tail -1 | tr -d '\r' | awk '{print $2}'); rm -f "$H"
[[ -n "$ADMIN_TOKEN" ]] || { echo "ERROR: admin login failed"; exit 1; }
ok "authenticated"

AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json')
api() {
  local m="$1" p="$2" body="${3:-}"
  if [[ -n "$body" ]]; then curl -sS -X "$m" "${AUTH[@]}" -d "$body" "$MM_API$p"
  else curl -sS -X "$m" "${AUTH[@]}" "$MM_API$p"; fi
}

# ------------------------------------------------------------------- bot ----
# `mmctl bot create` is blocked in local mode, so the bot is created over REST.
say "Creating bot '@${BOT_USERNAME}'"
BOT_USER_ID=$(jget "$(api GET "/users/username/$BOT_USERNAME")" "d['id']")
if [[ -z "$BOT_USER_ID" ]]; then
  BOT=$(api POST "/bots" "$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["BOT_USERNAME"],"display_name":"SprintFlow Assistant","description":"AI colleague for the SprintFlow workspace"}))')")
  BOT_USER_ID=$(jget "$BOT" "d['user_id']")
fi
[[ -n "$BOT_USER_ID" ]] || { echo "ERROR: could not create bot"; exit 1; }
export BOT_USER_ID
ok "bot user id ${BOT_USER_ID}"

# ------------------------------------------------- GATE: promote, then lock --
# Verified empirically on Team Edition 11.7.10: both of these work despite the
# "(EE Only)" text in `mmctl permissions --help`, which is stale (the unlicensed
# whitelist that once blocked it was removed in v6.0.0).
say "Promoting the bot to system_admin  [must precede the lockdown]"
mmctl roles system-admin "$BOT_USERNAME" 2>&1 | tail -1
BOT_ROLES=$(jget "$(api GET "/users/$BOT_USER_ID")" "d['roles']")
case " $BOT_ROLES " in
  *" system_admin "*) ok "bot roles: ${BOT_ROLES}" ;;
  *) echo "ERROR: bot was not promoted (roles: ${BOT_ROLES}). Aborting before lockdown."; exit 1 ;;
esac

say "Locking down team creation for regular users"
# Without system_admin the bot would lose create_team here too — hence the order.
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
  TEAM=$(api POST "/teams" "$(python3 -c 'import json,os;print(json.dumps({"name":os.environ["TEAM_NAME"],"display_name":os.environ["TEAM_DISPLAY"],"type":"O"}))')")
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
# Display names and headers, so the workspace reads like a real company rather
# than a fresh Mattermost install. Any channel in MM_DEFAULT_CHANNELS that is
# not listed here still gets created, just without a header.
channel_display() {
  case "$1" in
    town-square) echo "General" ;;
    *) python3 -c "import sys;print(sys.argv[1].replace('-',' ').title())" "$1" ;;
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

# Apply display name, header and purpose to an existing channel.
# The variables are exported rather than prefixed onto the call: a `VAR=x cmd`
# prefix does not reach a $(...) substitution inside cmd's arguments, because
# that substitution is evaluated by the parent shell first.
describe_channel() {
  local ch_id="$1" ch_name="$2"
  local header body
  export CH_DISPLAY CH_HEADER
  CH_DISPLAY="$(channel_display "$ch_name")"
  CH_HEADER="$(channel_header "$ch_name")"
  [[ -z "$CH_HEADER" ]] && return 0
  body="$(python3 -c 'import json,os;print(json.dumps({"display_name":os.environ["CH_DISPLAY"],"header":os.environ["CH_HEADER"],"purpose":os.environ["CH_HEADER"]}))')"
  api PUT "/channels/$ch_id/patch" "$body" >/dev/null
}

say "Creating default channels: ${DEFAULT_CHANNELS}"
for ch in $DEFAULT_CHANNELS; do
  CH_ID=$(jget "$(api GET "/teams/$TEAM_ID/channels/name/$ch")" "d['id']")
  if [[ -z "$CH_ID" ]]; then
    export CH_NAME="$ch"
    export CH_DISPLAY="$(channel_display "$ch")"
    BODY="$(python3 -c 'import json,os;print(json.dumps({"team_id":os.environ["TEAM_ID"],"name":os.environ["CH_NAME"],"display_name":os.environ["CH_DISPLAY"],"type":"O"}))')"
    CH_ID=$(jget "$(api POST "/channels" "$BODY")" "d['id']")
  fi
  [[ -z "$CH_ID" ]] && { warn "could not create #${ch}"; continue; }
  describe_channel "$CH_ID" "$ch"
  api POST "/channels/$CH_ID/members" "{\"user_id\":\"$BOT_USER_ID\"}" >/dev/null 2>&1 || true
  ok "#${ch} ready, described, bot joined"
done

# Mattermost will not let the default channel be deleted, and its slug is
# special-cased throughout the server (ExperimentalDefaultChannels always keeps
# it). So the slug stays `town-square` and only the display name changes —
# renaming the slug risks breaking the default-channel handling for no gain.
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
HOOK_JSON=$(api POST "/hooks/outgoing" "$(python3 -c '
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
}))')")
WEBHOOK_TOKEN=$(jget "$HOOK_JSON" "d['token']")
[[ -n "$WEBHOOK_TOKEN" ]] || { echo "ERROR: could not create outgoing webhook: $HOOK_JSON"; exit 1; }
ok "webhook created"

# ---------------------------------------------------------------- brand -----
# Mattermost rejects SVG: uploads are decoded by a codec set that covers png,
# jpeg, gif, bmp, tiff and webp only. prepare_branding.sh rasterises first.
say "Applying branding"
if [[ -x ./scripts/prepare_branding.sh ]]; then
  ./scripts/prepare_branding.sh >/dev/null 2>&1 || warn "branding rasterisation failed"
fi
if [[ -f branding/generated/login-logo.png ]]; then
  curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
       -F "image=@branding/generated/login-logo.png" "$MM_API/brand/image" >/dev/null && ok "login logo uploaded" \
       || warn "login logo upload failed"
fi
if [[ -f branding/generated/team-icon.png ]]; then
  curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
       -F "image=@branding/generated/team-icon.png" "$MM_API/teams/$TEAM_ID/image" >/dev/null && ok "team icon uploaded" \
       || warn "team icon upload failed"
fi

# ------------------------------------------------------------ write .env ----
say "Writing tokens into .env"
python3 - "$ENV_FILE" "$BOT_TOKEN" "$WEBHOOK_TOKEN" <<'PY'
import re, sys
path, bot, hook = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
for key, val in (("MATTERMOST_BOT_TOKEN", bot), ("MATTERMOST_OUTGOING_WEBHOOK_TOKEN", hook)):
    if re.search(rf"(?m)^{key}=.*$", s):
        s = re.sub(rf"(?m)^{key}=.*$", f"{key}={val}", s)
    else:
        s += f"\n{key}={val}\n"
open(path, "w").write(s)
print("    updated MATTERMOST_BOT_TOKEN and MATTERMOST_OUTGOING_WEBHOOK_TOKEN")
PY

say "Restarting ai-core so it picks up the new tokens"
docker compose up -d --force-recreate --no-deps ai-core >/dev/null
ok "ai-core restarted"

cat <<SUMMARY

──────────────────────────────────────────────────────────────
 SprintFlow is ready.

   Mattermost   http://localhost:${MM_PORT}
   login        ${MM_ADMIN_USERNAME} / ${MM_ADMIN_PASSWORD}
   team         ${TEAM_DISPLAY}   channels  #${BOT_CHANNEL} ${DEFAULT_CHANNELS}
   ai-core      internal only (no published port)

 Regular users can no longer create teams; the bot is system_admin.

 Try it:   @${BOT_USERNAME} hello
 Verify:   ./scripts/smoke_test.sh && python3 scripts/verify_routing.py
──────────────────────────────────────────────────────────────
SUMMARY
