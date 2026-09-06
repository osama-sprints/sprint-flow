#!/usr/bin/env python3
"""Verify the supervisor orchestration end to end against the running stack.

Host-side, standard library only. Prints one PASS/FAIL line per assertion and
exits non-zero on any failure. In-container code is exercised by piping
``scripts/_routing_probe.py`` over stdin (never ``docker compose cp``).

What it proves, in order:

  0. warm-up DM so a stale confirmation from earlier testing cannot swallow a turn
  P. in-container probe: labelled routing corpus, routing latency distribution,
     tool-group containment, graph node names
  1. /metrics before and after N mixed-intent DMs as the admin: routing counter
     deltas per route, and sprintflow_routing_model_calls_total unchanged
     (escalation rate 0/N); per-turn end-to-end latency printed
  2. one multi-intent DM ("open sprint ... and tell me what's scheduled") -> exactly
     ONE bot reply that mentions the sprint AND the calendar; the sprint exists
  3. "hello there" -> exactly one reply (fallback still answers)
  4. existing suites scripts/verify_routing.py, verify_threading.py, verify_isolation.py
  5. in-flight: "schedule the standup ..." asks to confirm, "yes" resumes the paused
     specialist, the ceremony exists afterwards

Run with the stack up and bootstrapped, from the repository root:

    python3 scripts/verify_orchestration.py            # everything
    python3 scripts/verify_orchestration.py --skip-suites   # without step 4

Environment (read from the process, then from ./.env for any missing key):
MM_ADMIN_USERNAME, MM_ADMIN_PASSWORD, MM_ADMIN_EMAIL, MM_TEAM_NAME, MM_BOT_CHANNEL,
MATTERMOST_BOT_USERNAME, MATTERMOST_HOST_PORT.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "_routing_probe.py")
METRIC_DECISIONS = "sprintflow_routing_decisions_total"
METRIC_MODEL_CALLS = "sprintflow_routing_model_calls_total"
REPLY_TIMEOUT = 150
SETTLE_SECONDS = 20
STAMP = str(int(time.time()))
COHORT = f"Verify-Orch-{STAMP}"
SPRINT = f"Verify-{STAMP}"

# Mixed intents, all reads or greetings so the batch has no side effects, every
# cohort reference explicit so the model has nothing to ask back.
MIXED_DMS: List[Tuple[str, str]] = [
    ("learner_support", f"when is the next standup for cohort {COHORT}?"),
    ("learner_support", "list cohorts"),
    ("back_office", f"assign @{os.environ.get('MM_ADMIN_USERNAME', 'admin')} as scrum master of cohort {COHORT}"),
    ("learner_support", "what's the leave policy?"),
    ("general", "thanks!"),
    ("learner_support", f"what's on this week for cohort {COHORT}?"),
    ("learner_support", "I'm blocked on the docker setup, who do I ask?"),
]

results: List[bool] = []
latencies: List[Tuple[str, float]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Print one PASS/FAIL line."""
    results.append(bool(ok))
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    return bool(ok)


def load_dotenv_defaults() -> None:
    """Fill missing environment keys from ./.env (values only; no defaults for secrets)."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv_defaults()
API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ["MATTERMOST_BOT_USERNAME"]


def req(method: str, path: str, body: Any = None, token: Optional[str] = None) -> Tuple[Any, Dict[str, str]]:
    """Call the Mattermost API and return (decoded_body, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}"), dict(response.headers)


def in_container(args: List[str], stdin_path: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run python inside ai-core, optionally piping a helper over stdin."""
    command = ["docker", "compose", "exec", "-T", "ai-core", "/app/.venv/bin/python", *args]
    if stdin_path is None:
        return subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    with open(stdin_path, "rb") as helper:
        return subprocess.run(command, stdin=helper, capture_output=True, text=True, cwd=ROOT)


def probe(*args: str) -> Tuple[int, str]:
    """Run a sub-command of the in-container probe; returns (exit code, stdout)."""
    done = in_container(["-", *args], stdin_path=PROBE)
    if done.returncode != 0 and args and args[0] != "probe":
        print(f"        probe {args[0]} failed: {done.stderr.strip()[-400:]}")
    return done.returncode, done.stdout


def probe_json(*args: str) -> Dict[str, Any]:
    """Run a JSON-printing probe sub-command and parse its last stdout line."""
    code, out = probe(*args)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    if code != 0 or not lines:
        return {}
    return json.loads(lines[-1])


def read_metrics() -> Dict[str, float]:
    """Read /metrics from inside the container; returns {metric-with-labels: value}."""
    code = (
        "import urllib.request;"
        "print(urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=10).read().decode())"
    )
    done = in_container(["-c", code])
    values: Dict[str, float] = {}
    for line in done.stdout.splitlines():
        if not line.startswith("sprintflow_routing"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            values[name] = float(value)
        except ValueError:
            continue
    return values


def routing_totals(metrics: Dict[str, float]) -> Tuple[Dict[str, float], float]:
    """Sum the decision counter per route and the model-call counter."""
    per_route: Dict[str, float] = {}
    model_calls = 0.0
    for name, value in metrics.items():
        if name.startswith(METRIC_DECISIONS + "{"):
            match = re.search(r'route="([^"]+)"', name)
            route = match.group(1) if match else "?"
            per_route[route] = per_route.get(route, 0.0) + value
        elif name.startswith(METRIC_MODEL_CALLS):
            model_calls += value
    return per_route, model_calls


# --- Mattermost session --------------------------------------------------------------

_, headers = req(
    "POST",
    "/users/login",
    {"login_id": os.environ["MM_ADMIN_USERNAME"], "password": os.environ["MM_ADMIN_PASSWORD"]},
)
TOKEN = headers["Token"]
admin, _ = req("GET", "/users/me", token=TOKEN)
bot, _ = req("GET", "/users/username/" + BOT, token=TOKEN)
dm, _ = req("POST", "/channels/direct", [admin["id"], bot["id"]], token=TOKEN)
DM = dm["id"]


def send(text: str) -> Dict[str, Any]:
    """DM the bot as the admin."""
    post, _ = req("POST", "/posts", {"channel_id": DM, "message": text}, token=TOKEN)
    return post


def bot_replies_since(after_ts: int) -> List[Dict[str, Any]]:
    """Bot posts in the DM created after ``after_ts``, oldest first."""
    data, _ = req("GET", f"/channels/{DM}/posts?per_page=60", token=TOKEN)
    posts = [
        p
        for p in data["posts"].values()
        if p["user_id"] == bot["id"]
        and p["create_at"] > after_ts
        and not str(p.get("type") or "").startswith("system_")
    ]
    posts.sort(key=lambda p: p["create_at"])
    return posts


def ask(text: str, label: str, settle: int = 0) -> Tuple[List[Dict[str, Any]], float]:
    """Send a DM, wait for the first reply, optionally settle, return (replies, seconds to first reply)."""
    post = send(text)
    started = time.time()
    deadline = started + REPLY_TIMEOUT
    first: Optional[float] = None
    while time.time() < deadline:
        time.sleep(2)
        if bot_replies_since(post["create_at"]):
            first = time.time() - started
            break
    if settle:
        time.sleep(settle)
    replies = bot_replies_since(post["create_at"])
    elapsed = first if first is not None else float("nan")
    latencies.append((label, elapsed))
    preview = replies[0]["message"][:110].replace("\n", " ") if replies else "<none>"
    print(f"        {label:28} {elapsed:6.1f}s  replies={len(replies)}  {preview!r}")
    return replies, elapsed


# --- 0. warm-up ---------------------------------------------------------------------

print()
print("=" * 88)
print("==> 0. warm-up DM (flushes any stale confirmation left in this DM by earlier testing)")
ask("hello there", "warm-up")

# --- P. in-container probe -----------------------------------------------------------

print("==> P. in-container probe (routing corpus, latency, tool groups, graph shape)")
probe_code, probe_out = probe("probe")
for line in probe_out.splitlines():
    print("   " + line)
check("in-container probe passed", probe_code == 0, f"exit {probe_code}")

print(f"==> P. cohort {COHORT} with the admin as scrum master")
setup = probe_json(
    "setup-cohort", COHORT, admin["id"], admin["username"], os.environ.get("MM_ADMIN_EMAIL", admin["email"])
)
check("probe created the cohort and the scrum-master membership", bool(setup.get("cohort_id")), json.dumps(setup))

# --- 1. metrics around N mixed DMs ------------------------------------------------------

print(f"==> 1. /metrics before and after N={len(MIXED_DMS)} mixed-intent DMs as the admin")
before_routes, before_model = routing_totals(read_metrics())
for expected_route, text in MIXED_DMS:
    replies, _ = ask(text, f"[{expected_route}]")
    check(f"reply arrived for {text[:40]!r}", len(replies) >= 1)
after_routes, after_model = routing_totals(read_metrics())
deltas = {
    route: after_routes.get(route, 0.0) - before_routes.get(route, 0.0)
    for route in set(after_routes) | set(before_routes)
}
total_delta = sum(deltas.values())
print(f"        routing decisions delta per route: {json.dumps(deltas, sort_keys=True)}  total={total_delta:g}")
print(
    f"        model-call counter: before={before_model:g} after={after_model:g}  escalation rate {after_model - before_model:g}/{len(MIXED_DMS)}"
)
# Every fresh turn is one decision. A turn that answered a pending question
# instead (Command(resume=...)) is deliberately NOT a decision, so the delta may
# be below N when the model asked something back mid-batch; it can never exceed N.
check(
    f"routing decisions counter increased by {total_delta:g} for N={len(MIXED_DMS)} turns",
    1 <= total_delta <= len(MIXED_DMS),
    "no decision recorded, or more than N: is more than one ai-core running?",
)
if total_delta < len(MIXED_DMS):
    print(
        f"        note: {len(MIXED_DMS) - total_delta:g} turn(s) resumed a pending question instead of starting a turn"
    )
check("sprintflow_routing_model_calls_total did not increase (escalation rate 0/N)", after_model == before_model)
check("at least two different routes were used across the batch", sum(1 for v in deltas.values() if v > 0) >= 2)

# --- 2. multi-intent -----------------------------------------------------------------

print("==> 2. multi-intent DM: one coherent reply, sprint opened, calendar mentioned")
multi_text = f"open sprint {SPRINT} for cohort {COHORT} and tell me what's scheduled"
replies, _ = ask(multi_text, "multi-intent", settle=SETTLE_SECONDS)
if (
    len(replies) == 1
    and replies[0]["message"].rstrip().endswith("?")
    and SPRINT.lower() not in replies[0]["message"].lower()
):
    # The model asked for confirmation first (allowed for privileged actions); answer and re-check.
    print("        bot asked a question first; answering 'yes'")
    replies, _ = ask("yes", "multi-intent (confirm)", settle=SETTLE_SECONDS)
reply_text = " ".join(r["message"] for r in replies).lower()
check("exactly ONE bot reply to the multi-intent DM (20 s settle)", len(replies) == 1, f"got {len(replies)}")
check("the reply mentions the sprint", "sprint" in reply_text and SPRINT.lower() in reply_text)
check(
    "the reply mentions the calendar",
    any(
        w in reply_text
        for w in ("schedul", "calendar", "ceremon", "upcoming", "standup", "retro", "planning", "nothing")
    ),
)
sprint_row = probe_json("check-sprint", COHORT, SPRINT)
check("the sprint exists in the database", bool(sprint_row.get("exists")), json.dumps(sprint_row))

# --- 3. fallback ----------------------------------------------------------------------

print("==> 3. fallback still answers")
replies, _ = ask("hello there", "fallback", settle=12)
check("'hello there' gets exactly one reply", len(replies) == 1, f"got {len(replies)}")

# --- 5. in-flight confirmation ---------------------------------------------------------
# (runs before the slow suites so a transport problem there does not hide this result)

print("==> 5. in-flight: schedule -> confirmation question -> 'yes' -> ceremony exists")
replies, _ = ask(f"schedule the standup for tomorrow at 9am UTC for cohort {COHORT}", "schedule (asks)", settle=8)
asked = len(replies) == 1 and ("?" in replies[0]["message"] or "confirm" in replies[0]["message"].lower())
check("the scheduling turn asks the person to confirm (one reply, a question)", asked, f"got {len(replies)} replies")
replies, _ = ask("yes", "confirm (resumes)", settle=8)
check("'yes' gets exactly one reply", len(replies) == 1, f"got {len(replies)}")
calendar = probe_json("check-ceremonies", COHORT)
standups = [c for c in calendar.get("ceremonies", []) if c.get("type") == calendar.get("standup_key")]
check(
    "a daily_standup ceremony now exists for the cohort (paused turn resumed in place)",
    bool(standups),
    json.dumps(calendar),
)
check("the stored instant is timezone-aware", all(c.get("tz_aware") for c in standups) if standups else False)
if replies:
    check(
        "the confirmation reply reports the scheduling outcome",
        any(w in replies[0]["message"].lower() for w in ("scheduled", "standup", "booked", "confirmed")),
    )

# --- 4. existing suites ---------------------------------------------------------------

if "--skip-suites" in sys.argv:
    print("==> 4. existing suites skipped (--skip-suites)")
else:
    print("==> 4. existing suites (routing, threading, isolation)")
    for script in ("verify_routing.py", "verify_threading.py", "verify_isolation.py"):
        started = time.time()
        done = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", script)], cwd=ROOT, capture_output=True, text=True
        )
        tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr.strip()[-200:]
        check(f"{script} exit 0 ({time.time() - started:.0f}s) — {tail}", done.returncode == 0)

# --- summary ---------------------------------------------------------------------------

print()
print("==> per-turn latency (time to first bot reply, end to end incl. model calls)")
for label, seconds in latencies:
    print(f"        {label:28} {seconds:6.1f}s")
measured = [s for _, s in latencies if s == s]
if measured:
    measured.sort()
    print(
        f"        turns={len(measured)}  min={measured[0]:.1f}s  median={measured[len(measured) // 2]:.1f}s  max={measured[-1]:.1f}s"
    )
print("        (the supervisor's own share is the probe's microsecond figures above)")
print("=" * 88)
failed = results.count(False)
print(
    f"ORCHESTRATION OK — {len(results)} checks"
    if not failed
    else f"ORCHESTRATION FAILED — {failed}/{len(results)} checks failed"
)
sys.exit(1 if failed else 0)
