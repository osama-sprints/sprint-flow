#!/usr/bin/env python3
"""Verify that two threads in the SAME channel keep separate context.

Before the session-key fix, every thread in a channel shared one LangGraph
history, so the bot could answer using facts stated in an unrelated thread.

Run with the stack up and bootstrapped: python3 scripts/verify_isolation.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ["MATTERMOST_BOT_USERNAME"]


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
T = h["Token"]
bot, _ = req("GET", "/users/username/" + BOT, token=T)
team, _ = req("GET", "/teams/name/" + os.environ["MM_TEAM_NAME"], token=T)
ch, _ = req("GET", f"/teams/{team['id']}/channels/name/{os.environ.get('MM_BOT_CHANNEL','town-square')}", token=T)
CH = ch["id"]


def send(msg, root_id=""):
    body = {"channel_id": CH, "message": msg}
    if root_id:
        body["root_id"] = root_id
    p, _ = req("POST", "/posts", body, token=T)
    return p


def wait_reply(after_ts, root_id=None, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        d, _ = req("GET", f"/channels/{CH}/posts?per_page=60", token=T)
        hits = [p for p in d["posts"].values()
                if p["user_id"] == bot["id"] and p["create_at"] > after_ts
                and not str(p.get("type") or "").startswith("system_")
                and (root_id is None or p.get("root_id") == root_id)]
        if hits:
            hits.sort(key=lambda p: p["create_at"])
            return hits[-1]
    return None


MARK_A = "ZEBRA-ALPHA"
MARK_B = "WALRUS-BETA"

print("==> Thread A")
a = send(f"@{BOT} Reply with exactly this token: {MARK_A}")
ra = wait_reply(a["create_at"], root_id=a["id"])
print(f"    A: {(ra or {}).get('message','<none>')[:50]!r}")

print("==> Thread B (separate thread, same channel)")
b = send(f"@{BOT} Reply with exactly this token: {MARK_B}")
rb = wait_reply(b["create_at"], root_id=b["id"])
print(f"    B: {(rb or {}).get('message','<none>')[:50]!r}")

# Ground truth: inspect what each session actually holds in the checkpointer.
# This is the precise meaning of "context isolation" — thread B's conversation
# state must not contain thread A's messages. Asking the model instead would
# conflate this with mem0, which deliberately shares distilled facts per PERSON.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def session_text(session_id):
    """Dump one session's messages by piping the helper in over stdin.

    Deliberately not `docker compose cp` — a copied file lives only in the
    container's writable layer and vanishes on the next rebuild, which silently
    turns every assertion below into a comparison against an empty string.
    """
    with open(os.path.join(ROOT, "scripts", "_dump_session.py"), "rb") as helper:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "ai-core", "/app/.venv/bin/python", "-", session_id],
            stdin=helper, capture_output=True, text=True, cwd=ROOT,
        )
    if out.returncode != 0:
        raise SystemExit(f"could not read session {session_id}: {out.stderr.strip()[:300]}")
    if not out.stdout.strip():
        raise SystemExit(f"session {session_id} came back empty — the checkpointer holds nothing for it")
    return out.stdout

sa = session_text(f"{CH}:{a['id']}")
sb = session_text(f"{CH}:{b['id']}")

print()
print("=" * 78)
checks = [
    ("thread A state holds its own token",      MARK_A in sa),
    ("thread B state holds its own token",      MARK_B in sb),
    ("thread A state does NOT contain B",       MARK_B not in sa),
    ("thread B state does NOT contain A",       MARK_A not in sb),
]
ok = all(v for _, v in checks)
for label, v in checks:
    print(f"  {label:52} {'PASS' if v else 'FAIL'}")
print("=" * 78)
print("CONTEXT ISOLATION OK" if ok else "ISOLATION FAILED")
print()
print("Note: mem0 long-term memory is a SEPARATE layer, keyed per person, and")
print("deliberately carries distilled facts across threads. That is by design;")
print("run scripts/verify_memory.py to see it.")
sys.exit(0 if ok else 1)
