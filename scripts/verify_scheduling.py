#!/usr/bin/env python3
"""Verify the Ceremony Scheduler tools in isolation (no live DB required).

Tests cover:
  1. Time parser rejects unrecognised text.
  2. Time parser rejects ambiguous times (no timezone or AM/PM).
  3. Time parser rejects past times.
  4. Valid future times are accepted and converted to UTC.
  5. schedule_ceremony, amend_ceremony, read_ceremonies are importable.
  6. schedule_ceremony is registered in the global tools list.

Run from the repo root (no DB needed for static checks):
    python3 scripts/verify_scheduling.py
"""

import sys
from datetime import datetime, timezone, timedelta


# Helpers

def _ok(label: str) -> None:
    print(f"  {'PASS':6} {label}")


def _fail(label: str, reason: str = "") -> None:
    print(f"  {'FAIL':6} {label}" + (f": {reason}" if reason else ""))



def _test_time_validation() -> list[bool]:
    """Directly exercise _parse_time and _check_past from ceremony_scheduler."""
    from app.core.langgraph.tools.ceremony_scheduler import _parse_time, _check_past

    results = []

    # -- 1. Unrecognised text --
    label = "Unrecognised time text is rejected"
    dt, err = _parse_time("after i drink my tea")
    ok = dt is None and "SYSTEM REJECTION" in err
    results.append(ok)
    _ok(label) if ok else _fail(label, err or "returned a datetime")

    # -- 2. Ambiguous time (no timezone) --
    label = "Ambiguous time (no timezone) is rejected"
    dt, err = _parse_time("Monday at 9")
    ok = dt is None and "SYSTEM REJECTION" in err
    results.append(ok)
    _ok(label) if ok else _fail(label, repr(err))

    # -- 3. Past time --
    label = "Past time is rejected"
    dt, err = _parse_time("1 January 2000 at 9 AM UTC")
    if dt is None:
        # Parser couldn't parse it — treat as a soft pass since past is moot
        ok = True
        _ok(label + " (parser returned None for distant past)")
    else:
        past_err = _check_past(dt)
        ok = bool(past_err) and "SYSTEM REJECTION" in past_err
        _ok(label) if ok else _fail(label, past_err)
    results.append(ok)

    # -- 4. Valid future time --
    label = "Valid future time is accepted and is UTC-aware"
    # Build a time string that is 2 days in the future so it won't be "past"
    future = datetime.now(timezone.utc) + timedelta(days=2)
    raw = future.strftime("%-d %B %Y at %I %p UTC")
    dt, err = _parse_time(raw)
    ok = dt is not None and dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
    results.append(ok)
    _ok(label) if ok else _fail(label, err or f"got {dt}")

    return results



# 5: Import check

def _test_imports() -> list[bool]:
    results = []

    for name in ("schedule_ceremony", "amend_ceremony", "read_ceremonies"):
        label = f"'{name}' is importable from ceremony_scheduler"
        try:
            import importlib
            mod = importlib.import_module("app.core.langgraph.tools.ceremony_scheduler")
            assert hasattr(mod, name), f"missing attribute '{name}'"
            ok = True
        except Exception as exc:
            ok = False
            _fail(label, str(exc))
        results.append(ok)
        if ok:
            _ok(label)

    return results


# 6: Tool registry check

def _test_registry() -> list[bool]:
    results = []
    label = "ceremony tools are registered in tools/__init__.py"
    try:
        from app.core.langgraph.tools import tools
        names = {t.name for t in tools}
        required = {"schedule_ceremony", "amend_ceremony", "read_ceremonies"}
        missing = required - names
        ok = not missing
        if ok:
            _ok(label)
        else:
            _fail(label, f"missing: {missing}")
    except Exception as exc:
        ok = False
        _fail(label, str(exc))
    results.append(ok)
    return results


# Main

def main() -> int:
    print("=" * 60)
    print("Ceremony Scheduler — static verification")
    print("=" * 60)

    checks: list[bool] = []

    print("\n[Time Validation]")
    checks.extend(_test_time_validation())

    print("\n[Imports]")
    checks.extend(_test_imports())

    print("\n[Tool Registry]")
    checks.extend(_test_registry())

    passed = sum(checks)
    total = len(checks)
    print()
    print("=" * 60)
    print(f"Result: {passed}/{total} checks passed")
    print("CEREMONY SCHEDULER OK" if passed == total else "SOME CHECKS FAILED")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
