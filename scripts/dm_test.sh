#!/usr/bin/env bash
# End-to-end check for the DM path: open a direct channel with the bot, send a
# message, and wait for a reply. This exercises the WebSocket listener, which
# is the only transport that can see direct messages.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

MM_API="http://localhost:${MATTERMOST_HOST_PORT:-8065}/api/v4"
BOT_USERNAME="${MATTERMOST_BOT_USERNAME:-sprintflow-assistant}"
PROMPT="${1:-Hi! I am blocked on the billing migration. Give me one short suggestion.}"

jget() { python3 -c "
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
if isinstance(d, dict) and 'status_code' in d and 'message' in d: print(''); sys.exit()
try: print(eval(sys.argv[2]) or '')
except Exception: print('')
" "$1" "$2"; }

echo "==> Logging in as ${MM_ADMIN_USERNAME}"
H=$(mktemp)
curl -sS -D "$H" -o /dev/null -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,os;print(json.dumps({"login_id":os.environ["MM_ADMIN_USERNAME"],"password":os.environ["MM_ADMIN_PASSWORD"]}))')" \
  "$MM_API/users/login"
TOKEN=$(grep -i '^token:' "$H" | tail -1 | tr -d '\r' | awk '{print $2}'); rm -f "$H"
[[ -n "$TOKEN" ]] || { echo "login failed"; exit 1; }
AUTH=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')

ADMIN_ID=$(jget "$(curl -sS "${AUTH[@]}" "$MM_API/users/username/$MM_ADMIN_USERNAME")" "d['id']")
BOT_ID=$(jget "$(curl -sS "${AUTH[@]}" "$MM_API/users/username/$BOT_USERNAME")" "d['id']")

echo "==> Opening a direct channel with @${BOT_USERNAME}"
DM=$(curl -sS -X POST "${AUTH[@]}" "$MM_API/channels/direct" -d "[\"$ADMIN_ID\",\"$BOT_ID\"]")
DM_ID=$(jget "$DM" "d['id']")
[[ -n "$DM_ID" ]] || { echo "could not open DM: $DM"; exit 1; }
echo "    dm_channel=$DM_ID  (type D)"

echo "==> Sending DM: ${PROMPT}"
POST=$(curl -sS -X POST "${AUTH[@]}" "$MM_API/posts" \
  -d "$(PROMPT="$PROMPT" DM_ID="$DM_ID" python3 -c 'import json,os;print(json.dumps({"channel_id":os.environ["DM_ID"],"message":os.environ["PROMPT"]}))')")
POST_ID=$(jget "$POST" "d['id']")
[[ -n "$POST_ID" ]] || { echo "post failed: $POST"; exit 1; }

echo "==> Waiting up to 120s for the assistant to reply over the WebSocket"
for i in $(seq 1 40); do
  sleep 3
  REPLY=$(curl -sS "${AUTH[@]}" "$MM_API/channels/$DM_ID/posts?per_page=30" | BOT_ID="$BOT_ID" POST_ID="$POST_ID" python3 -c '
import json,os,sys
d=json.load(sys.stdin); posts=d.get("posts",{})
bot, trigger = os.environ["BOT_ID"], os.environ["POST_ID"]
after=posts.get(trigger,{}).get("create_at",0)
hits=[p for p in posts.values()
      if p.get("user_id")==bot and not (p.get("type") or "").startswith("system_")
      and p.get("create_at",0) > after]
hits.sort(key=lambda p:p["create_at"])
if hits: print(hits[-1]["message"])
')
  if [[ -n "$REPLY" ]]; then
    echo; echo "──────────────── DM REPLY RECEIVED ────────────────"
    echo "$REPLY"
    echo "───────────────────────────────────────────────────"
    echo "PASS: direct messages work."
    exit 0
  fi
  printf '.'
done
echo; echo "FAIL: no DM reply within 120s."
echo "  docker compose logs --tail=60 ai-core | grep mattermost_ws"
exit 1
