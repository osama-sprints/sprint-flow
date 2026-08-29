#!/usr/bin/env python3
"""Regression test for reply threading shape.

The bot should thread its replies in channels but not in 1-on-1s:

    public channel          -> threaded under the triggering post
    DM, plain message       -> flat, no root_id
    DM, inside a thread     -> stays in that thread

Run with the stack up and bootstrapped:  python3 scripts/verify_threading.py
"""

import json
import os
import sys
import time
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"


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

admin, _ = req("GET", "/users/username/" + os.environ["MM_ADMIN_USERNAME"], token=TOKEN)
bot, _ = req("GET", "/users/username/" + os.environ["MATTERMOST_BOT_USERNAME"], token=TOKEN)
team, _ = req("GET", "/teams/name/" + os.environ["MM_TEAM_NAME"], token=TOKEN)
public, _ = req("GET", f"/teams/{team['id']}/channels/name/{os.environ['MM_BOT_CHANNEL']}", token=TOKEN)
dm, _ = req("POST", "/channels/direct", [admin["id"], bot["id"]], token=TOKEN)


def send(channel_id, message, root_id=""):
    """Post a message as the admin."""
    body = {"channel_id": channel_id, "message": message}
    if root_id:
        body["root_id"] = root_id
    post, _ = req("POST", "/posts", body, token=TOKEN)
    return post


def await_reply(channel_id, after_ts, predicate, timeout=150):
    """Wait for a bot reply created after `after_ts` that satisfies `predicate`.

    Matching on the predicate rather than "the most recent reply" matters: a
    thread root and its in-thread reply are two separate messages, so the bot
    answers both and the replies can land in either order.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        data, _ = req("GET", f"/channels/{channel_id}/posts?per_page=60", token=TOKEN)
        for post in sorted(data["posts"].values(), key=lambda p: p["create_at"]):
            if post["user_id"] != bot["id"] or post["create_at"] <= after_ts:
                continue
            if str(post.get("type") or "").startswith("system_"):
                continue
            if predicate(post):
                return post
    return None


CASES = []

# 1. Plain DM -> flat reply.
p1 = send(dm["id"], "Threading case one: reply with OK.")
CASES.append(("DM, plain message", "flat (no root_id)", dm["id"], p1["create_at"], lambda p: p["root_id"] == ""))

# 2. Public channel -> threaded under the trigger.
p2 = send(public["id"], f"@{os.environ['MATTERMOST_BOT_USERNAME']} Threading case two: reply with OK.")
CASES.append(
    ("Public channel", "threaded under trigger", public["id"], p2["create_at"], lambda p: p["root_id"] == p2["id"])
)

# 3. DM where the human opened a thread -> the reply stays in that thread.
root = send(dm["id"], "Threading case three: thread root.")
p3 = send(dm["id"], "Threading case three: reply with OK.", root_id=root["id"])
CASES.append(
    ("DM, inside a thread", "stays in that thread", dm["id"], p3["create_at"], lambda p: p["root_id"] == root["id"])
)

print()
print("=" * 82)
ok = True
for label, expected, channel_id, after, predicate in CASES:
    reply = await_reply(channel_id, after, predicate)
    passed = reply is not None
    ok = ok and passed
    got = (reply["root_id"] or "(empty)") if reply else "no matching reply"
    print(f"  {label:22} expect {expected:24} root_id={got:28} {'PASS' if passed else 'FAIL'}")
print("=" * 82)
print("ALL THREADING CASES PASS" if ok else "SOME CASES FAILED")
sys.exit(0 if ok else 1)
