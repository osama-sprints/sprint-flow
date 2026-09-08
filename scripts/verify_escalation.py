#!/usr/bin/env python3
"""Verify the escalation handoff: the right human gets DM'd, the ticket is right, failures are graceful.

Runs the in-container probe (``scripts/_escalation_probe.py``, piped over
stdin so it never depends on a copied file) against the live database. The
probe fakes Mattermost entirely (see ``FakeMattermost`` in that file), so this
does not need the stack's Mattermost container to be reachable from the host
— only Postgres, through ai-core.

What is proved, in order (one PASS/FAIL line each, non-zero exit on any
failure):

  1. The tool's argument schema carries no ``ticket_type`` and no identity —
     routing cannot be steered by the model, and group membership is correct
     (learner-support only, never back-office).
  2. A cohort with a tech lead assigned: the ticket is created, routed and
     status ``waiting_human``; the human's DM contains the ticket ref and
     question; the learner-facing reply never names the human; a second
     cohort's tech lead receives nothing (cross-cohort scoping).
  3. A cohort with nobody in the required role: the ticket is still created
     (unassigned, ``open``), no DM is attempted, and the learner is told
     honestly rather than being led to believe someone is already on it.
  4. A human is assigned but the (faked) Mattermost call fails: the ticket
     keeps its assignment but stays ``open``, and the learner-facing message
     is equally honest about nobody having been reached yet.
  5. Two people holding the same role in one cohort: routing picks the
     earliest-assigned one, deterministically, not arbitrarily.
  6. Edge cases add no ticket row: no cohort resolves for the channel, the
     cohort is deactivated, the question is empty, the learner was never
     synced.
  7. ``learner_thread_id`` falls back to the channel id when the turn
     supplied none.

Run with the stack up, from the repository root:

    python3 scripts/verify_escalation.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__)).rsplit(os.sep + "scripts", 1)[0]
PROBE = os.path.join(ROOT, "scripts", "_escalation_probe.py")


def main() -> int:
    """Pipe the probe into the ai-core container and relay its result.

    Returns:
        int: 0 when every assertion passed.
    """
    print("==> Escalation handoff: in-container probe (service + tool wrapper, faked Mattermost)")
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
    all_passed = probe.returncode == 0 and probe.stdout.strip().endswith("ESCALATION PROBE OK")

    print()
    print("=" * 80)
    print(f"  {'probe ran and printed assertions':66} {'PASS' if ran else 'FAIL'}")
    print(f"  {'probe: every assertion passed':66} {'PASS' if all_passed else 'FAIL'}")
    print("=" * 80)

    ok = ran and all_passed
    print("ESCALATION OK" if ok else "ESCALATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
