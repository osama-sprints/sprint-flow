#!/usr/bin/env python3
"""Regression test for public-channel routing and thread continuity.

The two transports must partition the work exactly, with no message answered
twice and no channel chatter answered at all:

  A  public, "@bot ..." (mention first)   -> exactly ONE reply (webhook owns it)
  B  follow-up in that thread, no mention -> replies (thread continuity)
  C  public, no mention, not in a thread  -> silence
  D  reply in a thread the bot is not in  -> silence
  E  public, mention mid-sentence         -> replies (webhook would miss this)

Run with the stack up and bootstrapped:  python3 scripts/verify_routing.py
"""

import json
import os
import sys
import time
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ["MATTERMOST_BOT_USERNAME"]


def req(method, path, body=None, token=None):
    """Call the Mattermost API and return (decoded_body, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


_, headers = req(
    "POST",
    "/users/login",
    {"login_id": os.environ["MM_ADMIN_USERNAME"], "password": os.environ["MM_ADMIN_PASSWORD"]},
)
TOKEN = headers["Token"]
bot, _ = req("GET", "/users/username/" + BOT, token=TOKEN)
team, _ = req("GET", "/teams/name/" + os.environ["MM_TEAM_NAME"], token=TOKEN)
channel, _ = req("GET", f"/teams/{team['id']}/channels/name/{os.environ['MM_BOT_CHANNEL']}", token=TOKEN)
CH = channel["id"]


def send(message, root_id=""):
    """Post as the admin and return the created post."""
    body = {"channel_id": CH, "message": message}
    if root_id:
        body["root_id"] = root_id
    post, _ = req("POST", "/posts", body, token=TOKEN)
    return post


def bot_replies_since(after_ts):
    """Every non-system bot post in the channel created after `after_ts`."""
    data, _ = req("GET", f"/channels/{CH}/posts?per_page=100", token=TOKEN)
    return [
        p
        for p in data["posts"].values()
        if p["user_id"] == bot["id"]
        and p["create_at"] > after_ts
        and not str(p.get("type") or "").startswith("system_")
    ]


def expect_reply(after_ts, label, timeout=90):
    """Wait for at least one reply, then settle to catch a double-reply."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if bot_replies_since(after_ts):
            break
    # Let a second (wrong) reply land if the routing is broken.
    time.sleep(12)
    replies = bot_replies_since(after_ts)
    ok = len(replies) == 1
    print(f"  {label:44} expect 1 reply   got {len(replies)}   {'PASS' if ok else 'FAIL'}")
    return ok, replies


def expect_silence(after_ts, label, quiet_for=30):
    """Assert the bot stays quiet for `quiet_for` seconds."""
    time.sleep(quiet_for)
    replies = bot_replies_since(after_ts)
    ok = len(replies) == 0
    print(f"  {label:44} expect silence   got {len(replies)}   {'PASS' if ok else 'FAIL'}")
    if not ok:
        for r in replies:
            print(f"        unexpected: {r['message'][:70]!r}")
    return ok


print()
print("=" * 88)
results = []

# A — mention as the first word. The outgoing webhook owns this one.
a = send(f"@{BOT} Reply with exactly: ALPHA")
ok_a, replies_a = expect_reply(a["create_at"], "A  public, mention first word")
results.append(ok_a)

# B — thread continuity: follow up with NO mention at all.
thread_root = replies_a[0]["root_id"] or a["id"] if replies_a else a["id"]
b = send("Now reply with exactly: BRAVO", root_id=thread_root)
ok_b, _ = expect_reply(b["create_at"], "B  thread follow-up, no mention")
results.append(ok_b)

# C — ordinary channel chatter, no mention, not in a thread.
c = send("Just talking among ourselves, nothing for the bot here.")
results.append(expect_silence(c["create_at"], "C  no mention, not in a thread"))

# D — a thread the bot has never been part of.
d_root = send("Unrelated thread root, humans only.")
time.sleep(8)
d = send("Still unrelated, no bot involved.", root_id=d_root["id"])
results.append(expect_silence(d["create_at"], "D  thread the bot is not part of"))

# E — mention mid-sentence: the webhook's first-word matching cannot see this.
e = send(f"hey @{BOT} please reply with exactly: ECHO")
ok_e, _ = expect_reply(e["create_at"], "E  mention mid-sentence")
results.append(ok_e)

print("=" * 88)
print("ALL ROUTING CASES PASS" if all(results) else "SOME CASES FAILED")
sys.exit(0 if all(results) else 1)
