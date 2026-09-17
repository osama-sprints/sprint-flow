#!/usr/bin/env python3
"""Verify calendar integration: create, reschedule, cancellation, outage, disabled mode.

Pipes ``scripts/_calendar_probe.py`` over stdin into the running ai-core
container (never ``docker compose cp``, which vanishes on rebuild) and prints
one PASS/FAIL line per assertion, exiting non-zero on any failure.

Run from the repository root with the stack up:  python3 scripts/verify_calendar_integration.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "_calendar_probe.py")


def probe(*args: str) -> subprocess.CompletedProcess:
    """Pipe the probe over stdin into the ai-core container."""
    with open(PROBE, "rb") as helper:
        return subprocess.run(
            ["docker", "compose", "exec", "-T", "ai-core", "/app/.venv/bin/python", "-", *args],
            stdin=helper,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )


def main() -> int:
    print("==> Calendar integration: in-container probe (real service, mocked Google API boundary)")
    result = probe("checks")
    sys.stdout.write(result.stdout)
    if result.returncode not in (0, 1):
        sys.stdout.write(result.stderr[-2000:])
    if result.returncode == 0:
        print("\nAll calendar integration checks passed.")
        return 0
    print("\nSome calendar integration checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())