#!/usr/bin/env python3
"""Verify the proactive onboarding journey against the live stack, counting real DMs.

What is proved, in order (one PASS/FAIL line each, non-zero exit on any failure):

  1. The fake-client probe passes inside the container (retry, halt, concurrency, roles).
  2. A brand-new Mattermost registration receives exactly ONE welcome DM from the bot.
  3. Replaying the arrival (`start_journey` for the same user — the exact code path the
     `new_user` event runs; Mattermost offers no API to re-emit the event) produces no
     second greeting: the DM still holds exactly one bot post 30 s later.
  4. The follow-up survives a restart: its due time is moved to now, ai-core is restarted,
     and the DM then holds exactly TWO bot posts.
  5. An inactive cohort receives nothing: a cohort is created, the user added as a
     learner, the cohort deactivated and an orientation enqueued; the post count stays at two
     and the row stays pending.
  6. The bot never reacts to its own DMs: the count is stable for 20 s.

The actual DM post count is printed at every stage.

Run with the stack up and bootstrapped: python3 scripts/verify_onboarding_journey.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ.get("MATTERMOST_BOT_USERNAME", "sprintflow-assistant")
POLL = int(os.environ.get("ONBOARDING_POLL_INTERVAL_SECONDS", "30"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "_onboarding_probe.py")
STAMP = str(int(time.time()))
COHORT_NAME = f"verify-onb-{STAMP}"

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    """Record and print one PASS/FAIL line."""
    checks.append((label, ok))
    print(f"  {label:66} {'PASS' if ok else 'FAIL'}", flush=True)


def req(method, path, body=None, token=None):
    """Call the Mattermost API and return (decoded body, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


def login(login_id, password):
    """Log in to Mattermost and return the session token."""
    _, h = req("POST", "/users/login", {"login_id": login_id, "password": password})
    return h["Token"]


def probe(*args: str) -> tuple[int, str]:
    """Run the in-container probe by piping it over stdin (never `docker compose cp`)."""
    with open(PROBE, "rb") as helper:
        out = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "-e",
                "LOG_LEVEL=WARNING",
                "ai-core",
                "/app/.venv/bin/python",
                "-",
                *args,
            ],
            stdin=helper,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
    return out.returncode, out.stdout


def probe_json(*args: str) -> dict:
    """Run one probe command inside the container and parse its PROBE_JSON line."""
    code, out = probe(*args)
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("PROBE_JSON "):
            return json.loads(line[len("PROBE_JSON ") :])
    raise SystemExit(f"probe {' '.join(args)} returned no JSON (exit {code}): {out[-400:]}")


ADMIN = login(os.environ["MM_ADMIN_USERNAME"], os.environ["MM_ADMIN_PASSWORD"])
bot, _ = req("GET", "/users/username/" + BOT, token=ADMIN)

print("==> [1] Running the in-container probe (fake Mattermost, live database)")
code, out = probe()
print(out)
check("in-container probe: every scenario passed", code == 0 and "ONBOARDING PROBE OK" in out)

username = f"onbjourney{STAMP}"
email = f"{username}@test.local"
password = "Journey12345!"
print(f"==> [2] Registering {username} (as a real signup would)")
try:
    user, _ = req("POST", "/users", {"email": email, "username": username, "password": password})
except urllib.error.HTTPError:
    user, _ = req("POST", "/users", {"email": email, "username": username, "password": password}, token=ADMIN)
USER_ID = user["id"]
print(f"    user_id={USER_ID}")

TOKEN = login(username, password)
dm, _ = req("POST", "/channels/direct", [USER_ID, bot["id"]], token=TOKEN)
DM = dm["id"]


def bot_post_count() -> int:
    """Count the bot's non-system posts in the new person's DM channel."""
    d, _ = req("GET", f"/channels/{DM}/posts?per_page=200", token=TOKEN)
    return sum(
        1
        for p in d["posts"].values()
        if p["user_id"] == bot["id"] and not str(p.get("type") or "").startswith("system_")
    )


def wait_for_count(target: int, timeout: float, label: str) -> int:
    """Poll the DM until the bot post count reaches ``target`` or ``timeout`` seconds pass."""
    deadline = time.time() + timeout
    count = bot_post_count()
    while time.time() < deadline and count < target:
        time.sleep(3)
        count = bot_post_count()
    print(f"    {label}: bot posts in DM = {count}")
    return count


print("==> Waiting up to 90s for the welcome DM")
count = wait_for_count(1, 90, "after arrival")
check("welcome DM delivered (exactly one bot post)", count == 1)

print("==> [3] Replaying the arrival via start_journey for the same user")
replay = probe_json("replay", USER_ID)
print(
    f"    probe: created={replay.get('created')} steps={[(s['kind'], s['status']) for s in replay.get('steps', [])]}"
)
check("replayed arrival is a no-op (created=False)", replay.get("created") is False)
time.sleep(30)
count = bot_post_count()
print(f"    30s after replay: bot posts in DM = {count}")
check("no second greeting after the replay", count == 1)

print("==> [4] Making the follow-up due now, then restarting ai-core")
due = probe_json("due-follow-up", USER_ID)
print(f"    probe: {due}")
subprocess.run(["docker", "compose", "restart", "ai-core"], cwd=ROOT, check=False, capture_output=True)
count = wait_for_count(2, 2 * POLL + 60, "after restart")
check("follow-up delivered after the restart (exactly two bot posts)", count == 2)
steps = probe_json("steps", USER_ID).get("steps", [])
follow_up = next((s for s in steps if s["kind"] == "follow_up"), {})
check(
    "follow-up row marked sent with the post id",
    follow_up.get("status") == "sent" and bool(follow_up.get("mattermost_post_id")),
)

print(f"==> [5] Inactive cohort '{COHORT_NAME}': learner membership, kill switch on, orientation enqueued")
inactive = probe_json("inactive-cohort", USER_ID, COHORT_NAME)
print(f"    probe: {inactive}")
time.sleep(2 * POLL + 10)
count = bot_post_count()
print(f"    after {2 * POLL + 10}s: bot posts in DM = {count}")
check("inactive cohort: no onboarding DM was sent", count == 2)
steps = probe_json("steps", USER_ID).get("steps", [])
orientation = next((s for s in steps if s["kind"] == "orientation"), {})
check(
    "inactive cohort: orientation row still pending, unclaimed",
    orientation.get("status") == "pending" and not orientation.get("claimed_by"),
)

print("==> [6] Watching the DM for 20s: the bot must not reply to its own posts")
time.sleep(20)
count = bot_post_count()
print(f"    after 20s: bot posts in DM = {count}")
check("bot never reacted to its own DMs (count stable)", count == 2)

probe("cleanup", USER_ID, COHORT_NAME)

print()
print("=" * 80)
ok = all(v for _, v in checks)
for label, v in checks:
    print(f"  {label:66} {'PASS' if v else 'FAIL'}")
print("=" * 80)
print("ONBOARDING JOURNEY OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
