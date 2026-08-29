#!/usr/bin/env python3
"""Verify that a brand-new registration is auto-joined to the default team.

Mattermost has no configuration that joins a team on signup, so ai-core listens
for the server-wide `new_user` WebSocket event and adds the account itself.
Channels then come from ExperimentalDefaultChannels on team join.

Run with the stack up and bootstrapped: python3 scripts/verify_onboarding.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
TEAM = os.environ.get("MATTERMOST_DEFAULT_TEAM", "sprints-community")
WANT_CHANNELS = ["town-square"] + os.environ.get("MM_DEFAULT_CHANNELS", "qa support").split()


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


_, h = req("POST", "/users/login", {"login_id": os.environ["MM_ADMIN_USERNAME"],
                                    "password": os.environ["MM_ADMIN_PASSWORD"]})
ADMIN = h["Token"]
team, _ = req("GET", f"/teams/name/{TEAM}", token=ADMIN)

stamp = str(int(time.time()))
email = f"newbie{stamp}@test.local"
username = f"newbie{stamp}"

print(f"==> Registering {username} (as a real signup would)")
user, _ = req("POST", "/users", {"email": email, "username": username, "password": "Newbie12345!"})
print(f"    user_id={user['id']}")

print("==> Waiting up to 45s for ai-core to auto-join the team")
joined = False
for _ in range(15):
    time.sleep(3)
    try:
        req("GET", f"/teams/{team['id']}/members/{user['id']}", token=ADMIN)
        joined = True
        break
    except urllib.error.HTTPError:
        continue

print()
print("=" * 74)
print(f"  {'auto-joined team ' + TEAM:44} {'PASS' if joined else 'FAIL'}")

ok = joined
if joined:
    chans, _ = req("GET", f"/users/{user['id']}/teams/{team['id']}/channels", token=ADMIN)
    names = {c["name"] for c in chans}
    for want in WANT_CHANNELS:
        hit = want in names
        ok = ok and hit
        print(f"  {'auto-joined #' + want:44} {'PASS' if hit else 'FAIL'}")

# The lockdown must also hold for this brand-new ordinary user.
_, h2 = req("POST", "/users/login", {"login_id": username, "password": "Newbie12345!"})
try:
    req("POST", "/teams", {"name": f"nope{stamp}", "display_name": "Nope", "type": "O"}, token=h2["Token"])
    print(f"  {'team creation blocked for regular user':44} FAIL (it succeeded)")
    ok = False
except urllib.error.HTTPError as e:
    blocked = e.code == 403
    ok = ok and blocked
    print(f"  {'team creation blocked for regular user':44} {'PASS' if blocked else 'FAIL'} (HTTP {e.code})")

print("=" * 74)
print("ONBOARDING + LOCKDOWN OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
