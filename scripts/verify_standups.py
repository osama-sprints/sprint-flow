#!/usr/bin/env python3
"""Verify the proactive daily standup pipeline: prompt dispatch, reply ingestion, missed days.

Runs the in-container probe (``scripts/_standups_probe.py``, piped over stdin so
it never depends on a copied file) against the live database. The probe fakes
Mattermost entirely (see ``FakeMattermost`` in that file), so this does not
need the stack's Mattermost container to be reachable from the host — only
Postgres, through ai-core.

What is proved, in order (one PASS/FAIL line each, non-zero exit on any
failure):

  1. Delivery: ``ensure_prompt`` is idempotent per sprint/learner/day; a due
     prompt is claimed and delivered to the learner's DM channel as exactly one
     prompt post asking all three questions; the row is then ``dispatched``
     with the post and DM channel recorded.
  2. Ingestion: an in-thread reply is accepted; exactly one standup entry is
     created with verified parsed fields AND the raw reply stored verbatim; the
     prompt closes as ``answered``. A second same-day reply is a ``duplicate``
     (raw preserved, confirmation attempted); redelivering the exact same post
     is a no-op guarded by a unique constraint.
  3. Missed days: a stale day closes as ``missed`` — a fact about the prompt
     row, never a fabricated entry; a late reply for the closed day is still
     captured as ``late`` and leaves the prompt closed.
  4. The real ``StandupDispatcher.run_once`` pass (restricted to the probe's
     own learner) ensures, claims and delivers end-to-end with exactly one DM,
     and a delivered row is no longer claimable.
  5. Probe rows are cleaned up even on failure.

Run with the stack up, from the repository root:

    python3 scripts/verify_standups.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__)).rsplit(os.sep + "scripts", 1)[0]
PROBE = os.path.join(ROOT, "scripts", "_standups_probe.py")


def main() -> int:
    """Pipe the probe into the ai-core container and relay its result.

    Returns:
        int: 0 when every assertion passed.
    """
    print("==> Proactive standups: in-container probe (service + dispatcher, faked Mattermost)")
    with open(PROBE, "rb") as source:
        probe = subprocess.run(
            ["docker", "compose", "exec", "-T", "ai-core", "/app/.venv/bin/python", "-"],
            stdin=source,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
    sys.stdout.write(probe.stdout)
    if probe.returncode != 0:
        sys.stdout.write(probe.stderr[-2000:])

    lines = [line for line in probe.stdout.splitlines() if line.rstrip().endswith(("PASS", "FAIL"))]
    ran = probe.returncode in (0, 1) and len(lines) > 0
    all_passed = probe.returncode == 0 and probe.stdout.strip().endswith("STANDUPS PROBE OK")

    print()
    print("=" * 80)
    print(f"  {'probe ran and printed assertions':66} {'PASS' if ran else 'FAIL'}")
    print(f"  {'probe: every assertion passed':66} {'PASS' if all_passed else 'FAIL'}")
    print("=" * 80)

    ok = ran and all_passed
    print("STANDUPS OK" if ok else "STANDUPS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())