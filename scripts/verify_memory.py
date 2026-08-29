#!/usr/bin/env python3
"""Verify that mem0 long-term memory reads and writes through Qdrant.

Proves three things end to end:
  1. a fact stated in conversation is extracted and WRITTEN to Qdrant
     (checked against the Qdrant REST API directly, not via the bot);
  2. the vector actually lands in the configured collection;
  3. the fact is RECALLED in a different conversation, which can only happen
     through a Qdrant similarity search.

Run with the stack up and bootstrapped: python3 scripts/verify_memory.py
"""

import json
import os
import sys
import time
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ["MATTERMOST_BOT_USERNAME"]
QURL = os.environ["QDRANT_URL"].rstrip("/")
QKEY = os.environ["QDRANT_API_KEY"]
COLLECTION = os.environ.get("LONG_TERM_MEMORY_COLLECTION_NAME", "sprintflow_longterm_memory")

FACT = "My preferred deployment window is Tuesday at 09:00 UTC"
NEEDLE = "tuesday"


def mm(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


def qdrant(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(QURL + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("api-key", QKEY)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


_, h = mm("POST", "/users/login", {"login_id": os.environ["MM_ADMIN_USERNAME"],
                                   "password": os.environ["MM_ADMIN_PASSWORD"]})
T = h["Token"]
me, _ = mm("GET", "/users/me", token=T)
bot, _ = mm("GET", "/users/username/" + BOT, token=T)
team, _ = mm("GET", "/teams/name/" + os.environ["MM_TEAM_NAME"], token=T)
pub, _ = mm("GET", f"/teams/{team['id']}/channels/name/{os.environ.get('MM_BOT_CHANNEL','town-square')}", token=T)
dm, _ = mm("POST", "/channels/direct", [me["id"], bot["id"]], token=T)


def send(channel_id, message):
    p, _ = mm("POST", "/posts", {"channel_id": channel_id, "message": message}, token=T)
    return p


def wait_reply(channel_id, after_ts, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        d, _ = mm("GET", f"/channels/{channel_id}/posts?per_page=40", token=T)
        hits = [p for p in d["posts"].values()
                if p["user_id"] == bot["id"] and p["create_at"] > after_ts
                and not str(p.get("type") or "").startswith("system_")]
        if hits:
            hits.sort(key=lambda p: p["create_at"])
            return hits[-1]["message"]
    return ""


def memories_in_qdrant():
    """Every stored memory string in the collection."""
    try:
        res = qdrant("POST", f"/collections/{COLLECTION}/points/scroll",
                     {"limit": 200, "with_payload": True})
    except Exception as e:
        print(f"    qdrant scroll failed: {e}")
        return []
    return [str(p.get("payload", {}).get("data", "")) for p in res["result"]["points"]]


print(f"==> Collection: {COLLECTION}")
existed = COLLECTION in [c["name"] for c in qdrant("GET", "/collections")["result"]["collections"]]
print(f"    exists before this run: {existed}")

print(f"==> DM the bot a durable fact: {FACT!r}")
p1 = send(dm["id"], f"Please remember this about me: {FACT}.")
print(f"    bot: {wait_reply(dm['id'], p1['create_at'])[:90]!r}")

print("==> Waiting for mem0 to extract and write to Qdrant (fire-and-forget)")
stored = []
for _ in range(20):
    time.sleep(4)
    stored = memories_in_qdrant()
    if any(NEEDLE in m.lower() for m in stored):
        break

written = any(NEEDLE in m.lower() for m in stored)
print(f"    memories now in Qdrant: {len(stored)}")
for m in stored[:5]:
    print(f"      - {m[:80]}")

print("==> Ask in a DIFFERENT conversation (public channel thread)")
p2 = send(pub["id"], f"@{BOT} When is my preferred deployment window? Answer briefly.")
recall = wait_reply(pub["id"], p2["create_at"])
print(f"    bot: {recall[:120]!r}")

collection_ok = COLLECTION in [c["name"] for c in qdrant("GET", "/collections")["result"]["collections"]]
recalled = NEEDLE in recall.lower()

print()
print("=" * 78)
checks = [
    (f"collection '{COLLECTION}' exists in Qdrant", collection_ok),
    ("fact WRITTEN to Qdrant (verified via Qdrant API)", written),
    ("fact RECALLED in a separate conversation (Qdrant search)", recalled),
]
ok = all(v for _, v in checks)
for label, v in checks:
    print(f"  {label:60} {'PASS' if v else 'FAIL'}")
print("=" * 78)
print("MEM0 <-> QDRANT OK" if ok else "MEMORY CHECKS FAILED")
sys.exit(0 if ok else 1)
