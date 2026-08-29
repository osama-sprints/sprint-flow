#!/usr/bin/env bash
# End-to-end check: post a message in Mattermost as the admin, then wait for the
# SprintFlow Assistant to answer. Exercises the whole pipeline —
# webhook -> ai-core -> LiteLLM -> Mattermost REST API.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

MM_API="http://localhost:${MATTERMOST_HOST_PORT:-8065}/api/v4"
BOT_USERNAME="${MATTERMOST_BOT_USERNAME:-sprintflow-assistant}"
PROMPT="${1:-@${MATTERMOST_BOT_USERNAME:-sprintflow-assistant} In one short sentence, what is SprintFlow?}"

jget() { python3 -c "
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
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

TEAM_ID=$(jget "$(curl -sS "${AUTH[@]}" "$MM_API/teams/name/${MM_TEAM_NAME:-sprints-community}")" "d['id']")
CHANNEL_ID=$(jget "$(curl -sS "${AUTH[@]}" "$MM_API/teams/$TEAM_ID/channels/name/${MM_BOT_CHANNEL:-town-square}")" "d['id']")
BOT_ID=$(jget "$(curl -sS "${AUTH[@]}" "$MM_API/users/username/$BOT_USERNAME")" "d['id']")
echo "    channel=$CHANNEL_ID bot=$BOT_ID"

echo "==> Posting: ${PROMPT}"
POST=$(curl -sS -X POST "${AUTH[@]}" "$MM_API/posts" \
  -d "$(PROMPT="$PROMPT" CHANNEL_ID="$CHANNEL_ID" python3 -c 'import json,os;print(json.dumps({"channel_id":os.environ["CHANNEL_ID"],"message":os.environ["PROMPT"]}))')")
POST_ID=$(jget "$POST" "d['id']")
[[ -n "$POST_ID" ]] || { echo "post failed: $POST"; exit 1; }
echo "    post_id=$POST_ID"

echo "==> Waiting up to 120s for the assistant to reply"
for i in $(seq 1 40); do
  sleep 3
  REPLY=$(curl -sS "${AUTH[@]}" "$MM_API/channels/$CHANNEL_ID/posts?per_page=30" | BOT_ID="$BOT_ID" POST_ID="$POST_ID" python3 -c '
import json,os,sys
d=json.load(sys.stdin)
posts=d.get("posts",{})
bot, trigger = os.environ["BOT_ID"], os.environ["POST_ID"]
after=posts.get(trigger,{}).get("create_at",0)
# Only real posts from the bot created AFTER the trigger. Without the type and
# timestamp filters this matches the "sprintflow-assistant joined the team."
# system message and reports a false pass.
hits=[p for p in posts.values()
      if p.get("user_id")==bot and not (p.get("type") or "").startswith("system_")
      and p.get("create_at",0) > after]
hits.sort(key=lambda p:p["create_at"])
if hits: print(hits[-1]["message"])
')
  if [[ -n "$REPLY" ]]; then
    echo
    echo "──────────────── ASSISTANT REPLIED ────────────────"
    echo "$REPLY"
    echo "───────────────────────────────────────────────────"
    echo "PASS: end-to-end pipeline works."
    exit 0
  fi
  printf '.'
done

echo
echo "FAIL: no reply within 120s."
echo "Debug with:"
echo "  docker compose logs --tail=60 ai-core"
echo "  docker compose logs --tail=40 mattermost | grep -i webhook"
exit 1
