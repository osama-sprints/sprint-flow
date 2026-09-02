#!/usr/bin/env python3
"""Verify Task 5: idempotent greeting + durable follow-up for onboarding.

--> Target path in the repo: scripts/verify_task5_onboarding.py

Complements verify_onboarding.py (which covers team auto-join + lockdown).
This script checks the two properties the brief calls non-negotiable for
proactive onboarding:

  - greeting a new arrival is safe to attempt more than once (idempotency)
  - a scheduled follow-up survives an ai-core restart (durability)

Runs docker compose commands via subprocess rather than shelling out through
bash, so on Windows/Git Bash this sidesteps MSYS's argument path-mangling
entirely -- no MSYS2_ARG_CONV_EXCL needed here.

Requires the stack up and bootstrapped. Run from the repository root:

    python3 scripts/verify_task5_onboarding.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
POSTGRES_USER = os.environ.get("POSTGRES_USER", "sprintflow")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "sprintflow")


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


def compose_exec(service, *cmd, check=True):
    """Run a command inside a compose service.

    Uses subprocess's argv list form (no shell=True), so arguments reach
    Docker exactly as given -- this is what avoids the Git Bash path
    conversion problems seen when running the equivalent commands by hand.
    """
    full = ["docker", "compose", "exec", "-T", service, *cmd]
    return subprocess.run(full, capture_output=True, text=True, check=check)


def psql(sql: str) -> str:
    """Run one SQL statement against the sprintflow database and return its
    trimmed, unaligned, tuples-only output (-tA)."""
    result = compose_exec(
        "postgres", "psql", "-U", POSTGRES_USER, "-d", POSTGRES_DB, "-tA", "-c", sql
    )
    return result.stdout.strip()


def call_handle_arrival(user_id: str) -> None:
    code = (
        "import asyncio\n"
        "from app.services.onboarding import handle_arrival\n"
        f"asyncio.run(handle_arrival('{user_id}'))\n"
    )
    compose_exec("ai-core", "/app/.venv/bin/python", "-c", code, check=False)


stamp = str(int(time.time()))
email = f"task5test{stamp}@test.local"
username = f"task5test{stamp}"

print(f"==> Registering {username}")
user, _ = req(
    "POST", "/users", {"email": email, "username": username, "password": "Task5Test12345!"}
)
user_id = user["id"]
print(f"    user_id={user_id}")

print("==> Waiting up to 45s for the initial greeting to be recorded")
greeted = False
for _ in range(15):
    time.sleep(3)
    row = psql(f"SELECT greeted_at FROM onboarding_state WHERE user_id = '{user_id}';")
    if row:
        greeted = True
        break

print()
print("=" * 74)
print(f"  {'initial greeting recorded':44} {'PASS' if greeted else 'FAIL'}")
ok = greeted

print("==> Calling handle_arrival a second time for the same user (duplicate event)")
before = psql(
    f"SELECT followups_sent, greeted_at FROM onboarding_state WHERE user_id = '{user_id}';"
)
call_handle_arrival(user_id)
after = psql(
    f"SELECT followups_sent, greeted_at FROM onboarding_state WHERE user_id = '{user_id}';"
)
idempotent = before == after
print(f"  {'duplicate arrival is a no-op (idempotent)':44} {'PASS' if idempotent else 'FAIL'}")
ok = ok and idempotent

print("==> Forcing a follow-up to be due, then restarting ai-core")
psql(
    "UPDATE onboarding_state SET next_followup_due_at = now() - interval '1 minute' "
    f"WHERE user_id = '{user_id}';"
)
subprocess.run(["docker", "compose", "restart", "ai-core"], check=True)

print("==> Waiting up to 6 minutes for the poller to pick it up after restart")
delivered = False
for _ in range(36):
    time.sleep(10)
    sent = psql(f"SELECT followups_sent FROM onboarding_state WHERE user_id = '{user_id}';")
    if sent and sent != "0":
        delivered = True
        break

print(f"  {'follow-up survives a restart (durable)':44} {'PASS' if delivered else 'FAIL'}")
ok = ok and delivered

print("=" * 74)
print("TASK 5 ONBOARDING OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)