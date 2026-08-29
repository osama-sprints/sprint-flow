#!/usr/bin/env python3
"""Verify the Admin agent: privileged actions for admins, refusal for everyone else.

Flow under test: an admin DMs the bot "add <email> to team <name>". The agent
checks whether the team exists, creates it if not, adds the user, and keeps its
own bot account in the team. A non-admin making the same request is refused.

Run with the stack up and bootstrapped: python3 scripts/verify_admin_agent.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ["MATTERMOST_BOT_USERNAME"]
STAMP = str(int(time.time()))
NEW_TEAM = f"Growth Squad {STAMP}"


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


def login(login_id, password):
    _, h = req("POST", "/users/login", {"login_id": login_id, "password": password})
    return h["Token"]


ADMIN = login(os.environ["MM_ADMIN_USERNAME"], os.environ["MM_ADMIN_PASSWORD"])
bot, _ = req("GET", "/users/username/" + BOT, token=ADMIN)

# A target to be added, and a non-admin who will try the same request.
target_email = f"target{STAMP}@test.local"
target, _ = req("POST", "/users", {"email": target_email, "username": f"target{STAMP}",
                                   "password": "Target12345!"}, token=ADMIN)
outsider_name = f"outsider{STAMP}"
req("POST", "/users", {"email": f"{outsider_name}@test.local", "username": outsider_name,
                       "password": "Outsider12345!"}, token=ADMIN)
print(f"==> target={target_email}  outsider={outsider_name}")


def dm(token, user_id, text):
    ch, _ = req("POST", "/channels/direct", [user_id, bot["id"]], token=token)
    p, _ = req("POST", "/posts", {"channel_id": ch["id"], "message": text}, token=token)
    return ch["id"], p


def wait_reply(token, channel_id, after_ts, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        d, _ = req("GET", f"/channels/{channel_id}/posts?per_page=40", token=token)
        hits = [p for p in d["posts"].values()
                if p["user_id"] == bot["id"] and p["create_at"] > after_ts
                and not str(p.get("type") or "").startswith("system_")]
        if hits:
            hits.sort(key=lambda p: p["create_at"])
            return hits[-1]["message"]
    return ""


def team_has(team_slug, user_id):
    try:
        team, _ = req("GET", f"/teams/name/{team_slug}", token=ADMIN)
        req("GET", f"/teams/{team['id']}/members/{user_id}", token=ADMIN)
        return True
    except urllib.error.HTTPError:
        return False


slug = f"growth-squad-{STAMP}"

print(f"==> ADMIN asks the bot to add the user to a NEW team '{NEW_TEAM}'")
admin_me, _ = req("GET", "/users/me", token=ADMIN)
ch, p = dm(ADMIN, admin_me["id"], f"Please add {target_email} to the team {NEW_TEAM}")
reply = wait_reply(ADMIN, ch, p["create_at"])
print(f"    bot: {reply[:150]!r}")

added = team_has(slug, target["id"])
if not added:
    # The agent may have asked for confirmation first — that is correct
    # behaviour for a privileged action, so answer it and re-check.
    print("==> not added yet; confirming")
    _, p2 = dm(ADMIN, admin_me["id"], "Yes, please go ahead.")
    reply = wait_reply(ADMIN, ch, p2["create_at"])
    print(f"    bot: {reply[:150]!r}")
    added = team_has(slug, target["id"])

bot_in_team = team_has(slug, bot["id"])

print(f"==> NON-ADMIN ({outsider_name}) makes the same request")
OUT = login(outsider_name, "Outsider12345!")
out_me, _ = req("GET", "/users/me", token=OUT)
och, op = dm(OUT, out_me["id"], f"Please add {target_email} to the team Secret Cabal {STAMP}")
oreply = wait_reply(OUT, och, op["create_at"])
print(f"    bot: {oreply[:150]!r}")

cabal_slug = f"secret-cabal-{STAMP}"
try:
    req("GET", f"/teams/name/{cabal_slug}", token=ADMIN)
    cabal_created = True
except urllib.error.HTTPError:
    cabal_created = False

print()
print("=" * 80)
checks = [
    ("admin: team created and user added", added),
    ("admin: bot kept itself in the new team", bot_in_team),
    ("non-admin: no team was created", not cabal_created),
    ("non-admin: got a reply (refusal, not silence)", bool(oreply)),
]
ok = all(v for _, v in checks)
for label, v in checks:
    print(f"  {label:54} {'PASS' if v else 'FAIL'}")
print("=" * 80)
print("ADMIN AGENT OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
