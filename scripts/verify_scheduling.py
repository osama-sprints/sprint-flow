#!/usr/bin/env python3
"""Verify ceremony scheduling: service, tools, confirmation, persistence, and a real conversation.

Two layers, both printing one PASS/FAIL line per assertion and exiting non-zero
on any failure:

1. In-container probe (always): ``scripts/_scheduling_probe.py`` is piped over
   stdin into the ai-core container. It seeds identities and a cohort (prefix
   ``verify-sched-``), binds ``current_requester`` and drives the tools inside a
   tiny LangGraph graph so ``interrupt()`` / ``Command(resume=...)`` run for real
   without a model: ambiguous time -> question and zero rows; unauthorised ->
   refused and zero rows; "no" -> zero rows; "yes" -> one row at the intended UTC
   instant; conflict -> refused; amend -> amendment trail; past ceremony frozen;
   member reads, non-member refused. It cleans up after itself.

2. Live conversation (when the stack is up and bootstrapped): the admin DMs the
   bot a real scheduling sentence for a cohort the probe created (the admin is
   given a scrum_master membership in it first; being on ADMIN_EMAILS they are a
   superadmin anyway, so the membership documents the cohort-role path rather
   than being required). The bot must answer with a confirmation naming an
   absolute time in UTC; the admin replies ``yes``; the probe must then find
   exactly one ceremony whose ``scheduled_at`` equals the confirmed instant. A
   second DM with no am/pm must produce a clarifying question and no new row.

Run from the repository root with the stack up:  python3 scripts/verify_scheduling.py
Skip the conversation layer:                      python3 scripts/verify_scheduling.py --probe-only
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import (
    datetime,
    timezone,
)
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "_scheduling_probe.py")
API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
BOT = os.environ.get("MATTERMOST_BOT_USERNAME", "sprintflow-assistant")
STAMP = str(int(time.time()))
UTC_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC")

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    checks.append((label, ok))
    suffix = f"  ({detail[:200]})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}")


# --- layer 1: the in-container probe ------------------------------------------------


def probe(*args: str) -> subprocess.CompletedProcess:
    """Pipe the probe over stdin (never `docker compose cp`, which vanishes on rebuild)."""
    with open(PROBE, "rb") as helper:
        return subprocess.run(
            ["docker", "compose", "exec", "-T", "ai-core", "/app/.venv/bin/python", "-", *args],
            stdin=helper,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )


def probe_json(*args: str) -> dict:
    """Run a probe mode that prints one JSON line and parse it."""
    out = probe(*args)
    if out.returncode != 0:
        raise SystemExit(f"probe {args[0]} failed: {out.stderr.strip()[-600:]}")
    # The container logs to stdout too, so take the last JSON line rather than the last line.
    json_lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    return json.loads(json_lines[-1]) if json_lines else {}


print("==> Layer 1: in-container probe (service + tools + interrupt, no model)")
result = probe("checks")
sys.stdout.write(result.stdout)
if result.returncode != 0:
    sys.stdout.write(result.stderr[-1500:])
probe_lines = [
    line for line in result.stdout.splitlines() if line.rstrip().endswith(("PASS", "FAIL")) or " FAIL  (" in line
]
check(
    "in-container probe ran and printed assertions",
    result.returncode in (0, 1) and bool(probe_lines),
    result.stderr[-300:],
)
check("in-container probe: every assertion passed", result.returncode == 0)

if "--probe-only" in sys.argv:
    print("=" * 84)
    ok = all(v for _, v in checks)
    print("SCHEDULING OK (probe only)" if ok else "SCHEDULING FAILED")
    sys.exit(0 if ok else 1)


# --- layer 2: the real conversation --------------------------------------------------


def req(method, path, body=None, token=None):
    """Call the Mattermost REST API and return (json, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}"), dict(resp.headers)


def login(login_id, password):
    """Log in and return the session token."""
    _, h = req("POST", "/users/login", {"login_id": login_id, "password": password})
    return h["Token"]


try:
    ADMIN = login(os.environ["MM_ADMIN_USERNAME"], os.environ["MM_ADMIN_PASSWORD"])
except (KeyError, urllib.error.URLError, OSError) as exc:
    print(f"==> Layer 2 skipped: Mattermost not reachable or MM_ADMIN_* unset ({exc})")
    print("=" * 84)
    ok = all(v for _, v in checks)
    print("SCHEDULING OK (probe only)" if ok else "SCHEDULING FAILED")
    sys.exit(0 if ok else 1)

bot, _ = req("GET", "/users/username/" + BOT, token=ADMIN)
admin_me, _ = req("GET", "/users/me", token=ADMIN)

# The person's Mattermost profile zone is what the bot interprets "2 pm" in. Set
# it explicitly so the expected instant is deterministic; the identity sync reads
# the profile (cached up to IDENTITY_PROFILE_CACHE_TTL seconds).
ZONE = "Europe/Berlin"
req(
    "PUT",
    f"/users/{admin_me['id']}/patch",
    {"timezone": {"useAutomaticTimezone": "false", "manualTimezone": ZONE, "automaticTimezone": ""}},
    token=ADMIN,
)

setup = probe_json("setup", STAMP, admin_me["id"], admin_me["username"], admin_me["email"])
COHORT = setup["cohort_name"]
print(
    f"==> Layer 2: cohort {COHORT} created; admin user_id={setup['user_id']} superadmin={setup['is_superadmin']} "
    f"scrum_master membership added"
)


def dm(text):
    """Send the bot a direct message as the admin; return (channel id, post)."""
    ch, _ = req("POST", "/channels/direct", [admin_me["id"], bot["id"]], token=ADMIN)
    p, _ = req("POST", "/posts", {"channel_id": ch["id"], "message": text}, token=ADMIN)
    return ch["id"], p


def wait_reply(channel_id, after_ts, timeout=150):
    """Poll the DM channel for the bot's next reply after a timestamp."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        d, _ = req("GET", f"/channels/{channel_id}/posts?per_page=40", token=ADMIN)
        hits = [
            p
            for p in d["posts"].values()
            if p["user_id"] == bot["id"]
            and p["create_at"] > after_ts
            and not str(p.get("type") or "").startswith("system_")
        ]
        if hits:
            hits.sort(key=lambda p: p["create_at"])
            return hits[-1]["message"]
    return ""


def ceremonies():
    """Every ceremony of the verification cohort, as the database holds them."""
    return probe_json("inspect", COHORT)["ceremonies"]


try:
    sentence = f"schedule sprint planning for tomorrow at 2 pm for cohort {COHORT}"
    print(f"==> admin: {sentence!r}")
    ch, p = dm(sentence)
    reply = wait_reply(ch, p["create_at"])
    print(f"    bot: {reply[:300]!r}")
    if "timezone" in reply.lower() and "UTC" not in reply:
        # The profile zone had not reached the bot (cached profile). State the
        # zone in the sentence instead: the same interpreter path, explicit zone.
        print("==> bot asked for a timezone (profile cache); retrying with an explicit zone in the sentence")
        sentence = f"schedule sprint planning for tomorrow at 2 pm {ZONE} for cohort {COHORT}"
        ch, p = dm(sentence)
        reply = wait_reply(ch, p["create_at"])
        print(f"    bot: {reply[:300]!r}")
    confirmed_utc = UTC_RE.search(reply)
    check("bot asks to confirm a specific absolute time (contains 'UTC')", bool(confirmed_utc), reply[:200])
    check("confirmation also shows the time in the person's zone", f"({ZONE})" in reply, reply[:200])
    check("nothing stored before the person confirms", len(ceremonies()) == 0, json.dumps(ceremonies()))

    print("==> admin: 'yes'")
    ch, p2 = dm("yes")
    reply2 = wait_reply(ch, p2["create_at"])
    print(f"    bot: {reply2[:300]!r}")
    rows = ceremonies()
    check("exactly one ceremony exists after 'yes'", len(rows) == 1, json.dumps(rows))
    check(
        "stored scheduled_at equals the confirmed UTC instant",
        len(rows) == 1 and bool(confirmed_utc) and rows[0]["scheduled_at_utc"] == f"{confirmed_utc.group(1)} UTC",
        json.dumps(rows),
    )
    check(
        "stored ceremony is sprint_planning, status scheduled",
        len(rows) == 1 and rows[0]["type"] == "sprint_planning" and rows[0]["status"] == "scheduled",
        json.dumps(rows),
    )
    check(
        "stored organiser is the admin's users row",
        len(rows) == 1 and rows[0]["organizer_id"] == setup["user_id"],
        json.dumps(rows),
    )

    # --- amend the ceremony through conversation (s1e4 acceptance criterion) ---
    ceremony_id = rows[0]["id"] if rows else None
    original_utc = rows[0]["scheduled_at_utc"] if rows else ""
    # The stored instant is "YYYY-MM-DD HH:MM UTC"; the ceremony's local day in
    # the organiser's zone is what a person would name when moving it.
    local_day = (
        datetime.strptime(original_utc, "%Y-%m-%d %H:%M UTC")
        .replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo(ZONE))
        .strftime("%Y-%m-%d")
    )

    # First, an amendment with no day: the interpreter must ask rather than
    # assume the ceremony's own date, and must change nothing.
    sentence = f"move ceremony #{ceremony_id} to 4 pm the same day"
    print(f"==> admin: {sentence!r}")
    ch, pv = dm(sentence)
    reply_v = wait_reply(ch, pv["create_at"])
    print(f"    bot: {reply_v[:300]!r}")
    check(
        "amendment with no explicit day -> bot asks for the day (no silent guess)",
        "?" in reply_v or "please give the day" in reply_v.lower(),
        reply_v[:200],
    )
    check(
        "the vague amendment changed nothing",
        len(ceremonies()) == 1 and ceremonies()[0]["scheduled_at_utc"] == original_utc,
        json.dumps(ceremonies()),
    )

    # Then the same amendment with the day the bot asked for.
    sentence = f"move ceremony #{ceremony_id} to {local_day} 16:00 {ZONE}"
    print(f"==> admin: {sentence!r}")
    ch, pa = dm(sentence)
    reply_a = wait_reply(ch, pa["create_at"])
    print(f"    bot: {reply_a[:300]!r}")
    # The amendment question names both instants ("move it from <old> to <new>"),
    # so the NEW one is the last UTC timestamp in the sentence.
    amend_instants = UTC_RE.findall(reply_a)
    amend_utc = f"{amend_instants[-1]} UTC" if amend_instants else None
    check("amendment asks to confirm the NEW absolute time", bool(amend_utc), reply_a[:200])
    check(
        "the amendment question names both the old and the new instant",
        len(amend_instants) >= 2 and f"{amend_instants[0]} UTC" == original_utc,
        f"{amend_instants} vs original {original_utc}",
    )
    check(
        "the new instant differs from the old one",
        bool(amend_utc) and amend_utc != original_utc,
        f"{amend_utc} vs {original_utc}",
    )
    check(
        "nothing changed before the person confirms the amendment",
        len(ceremonies()) == 1 and ceremonies()[0]["scheduled_at_utc"] == original_utc,
        json.dumps(ceremonies()),
    )

    print("==> admin: 'yes'")
    ch, pa2 = dm("yes")
    reply_a2 = wait_reply(ch, pa2["create_at"])
    print(f"    bot: {reply_a2[:300]!r}")
    amended = ceremonies()
    check("still exactly one ceremony after the amendment", len(amended) == 1, json.dumps(amended))
    check(
        "stored scheduled_at equals the newly confirmed UTC instant",
        len(amended) == 1 and bool(amend_utc) and amended[0]["scheduled_at_utc"] == amend_utc,
        json.dumps(amended),
    )
    check(
        "the amendment is traceable (a scheduled_at amendment row exists, by the admin)",
        len(amended) == 1
        and any(a["field"] == "scheduled_at" and a["by"] == setup["user_id"] for a in amended[0]["amendments"]),
        json.dumps(amended),
    )

    # --- read the calendar through conversation (open to any cohort member) ---
    sentence = f"what's scheduled for cohort {COHORT}?"
    print(f"==> admin: {sentence!r}")
    ch, pr = dm(sentence)
    reply_r = wait_reply(ch, pr["create_at"])
    print(f"    bot: {reply_r[:300]!r}")
    check(
        "reading the calendar in conversation lists the ceremony at its amended time",
        "16:00" in reply_r.replace("\u202f", " "),
        reply_r[:250],
    )

    sentence = f"schedule the retro for tomorrow at 2 for cohort {COHORT}"
    print(f"==> admin: {sentence!r}")
    ch, p3 = dm(sentence)
    reply3 = wait_reply(ch, p3["create_at"])
    print(f"    bot: {reply3[:300]!r}")
    lowered = reply3.lower()
    check(
        "ambiguous 'at 2' -> bot asks a clarifying question (am/pm)",
        ("afternoon" in lowered or "morning" in lowered or "am" in lowered and "pm" in lowered) and "?" in reply3,
        reply3[:200],
    )
    rows_after = ceremonies()
    check("ambiguous request created no new row", len(rows_after) == 1, json.dumps(rows_after))
    check("the ambiguous request did not ask for confirmation", "Please confirm" not in reply3, reply3[:200])
finally:
    print("==> cleanup")
    print(f"    {probe_json('cleanup', STAMP)}")

print()
print("=" * 84)
for label, ok in checks:
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}")
print("=" * 84)
ok = all(v for _, v in checks)
print("SCHEDULING OK" if ok else "SCHEDULING FAILED")
sys.exit(0 if ok else 1)
