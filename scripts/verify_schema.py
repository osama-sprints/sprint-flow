#!/usr/bin/env python3
"""Verify the SprintFlow data model against the running stack, on a FRESH database.

Standard library only. Run from the repository root with the stack up:

    python3 scripts/verify_schema.py            # full run, including the assistant smoke test
    SKIP_SMOKE=1 python3 scripts/verify_schema.py

What it proves, one PASS/FAIL line per assertion, exit 1 on any failure:

  1. A throwaway database ``sprintflow_verify_<stamp>`` is created inside the
     compose Postgres, with a fake ``checkpoints`` table holding one row — the
     LangGraph checkpointer's pre-existing table, imitated.
  2. ``alembic upgrade head`` brings it to the full schema (exit 0); a second
     run is a no-op (exit 0, no upgrade executed).
  3. The reference-data seed runs twice through the documented command with no
     manual PYTHONPATH; role and ceremony-type counts do not change.
  4. The in-container probe ``ai-core/scripts/verify_schema.py`` is piped over
     stdin (never ``docker compose cp``) and its PASS/FAIL lines are folded in:
     columns, named constraints, timestamptz everywhere, NULLS NOT DISTINCT,
     one person with two roles in two cohorts, idempotent upserts, overlap
     boundaries, ticket references, the onboarding outbox.
  5. Populated cycle: rows are inserted with psql (user, two cohorts, two
     memberships with different roles, sprint, ceremony), a second role in the
     same cohort is rejected by the database, ``alembic downgrade base`` exits 0
     and removes ONLY the domain tables (the fake ``checkpoints`` row survives),
     ``alembic upgrade head`` exits 0 again, the population is re-applied on
     the fresh head and ``alembic check`` reports no drift.
  6. Live database: ``alembic check`` reports no drift, ``alembic current`` is
     at head, all four checkpointer tables and all domain tables exist, and
     ``/health`` reports the domain schema healthy.
  7. The assistant still answers after migrating: ``scripts/smoke_test.sh``
     exit code is folded in, and the live ``checkpoints`` table is populated
     afterwards.
  8. The throwaway database is dropped in a ``finally`` — also on failure.

Environment is read from ``os.environ`` (the Makefile sources ``.env``); when
run by hand, ``.env`` at the repository root is loaded for any variable that
is not already set.
"""

import json
import os
import subprocess
import sys
import time
from typing import Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "ai-core", "scripts", "verify_schema.py")
HEAD_REVISION = "0002_sprints_name_ci"
DOMAIN_TABLES = (
    "users",
    "roles",
    "ceremony_types",
    "cohorts",
    "cohort_memberships",
    "sprints",
    "ceremonies",
    "ceremony_amendments",
    "daily_standups",
    "escalation_tickets",
    "onboarding_steps",
)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")
EXPECTED_ROLES = 4
EXPECTED_CEREMONY_TYPES = 5

results: list[bool] = []


def load_dotenv() -> None:
    """Fill os.environ from ``.env`` for keys not already set (never overrides)."""
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
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail.strip()[:200]})" if detail and not ok else ""
    print(f"  {label:74} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)
    return ok


def compose(args: Sequence[str], stdin=None, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run ``docker compose <args>`` from the repository root."""
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def psql(database: str, sql: str) -> subprocess.CompletedProcess:
    """Run one SQL string with psql inside the postgres container, tuples only."""
    return compose(
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            PG_USER,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
            "-c",
            sql,
        ]
    )


def scalar(database: str, sql: str) -> str:
    """Return the first line of a psql query, or ``ERROR: ...`` on failure."""
    proc = psql(database, sql)
    if proc.returncode != 0:
        return f"ERROR: {proc.stderr.strip()}"
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def ai_core(database: str, command: Sequence[str], extra_env: dict[str, str] | None = None, stdin=None):
    """Run a command in the ai-core container against ``database`` with migrations-on-start off."""
    env_args = ["-e", f"POSTGRES_DB={database}", "-e", "AI_CORE_MIGRATE_ON_START=false"]
    for key, value in (extra_env or {}).items():
        env_args += ["-e", f"{key}={value}"]
    return compose(["exec", "-T", *env_args, "ai-core", *command], stdin=stdin)


def alembic(database: str, *args: str) -> subprocess.CompletedProcess:
    """Run an Alembic command inside the ai-core container against ``database``."""
    return ai_core(database, ["/app/.venv/bin/alembic", *args])


def count_domain_tables(database: str) -> int:
    """Count domain tables."""
    names = ",".join(f"'{t}'" for t in DOMAIN_TABLES)
    value = scalar(
        database,
        f"SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ({names})",
    )
    return int(value) if value.isdigit() else -1


def table_exists(database: str, table: str) -> bool:
    """Whether ``table`` exists in ``database`` (asked through psql)."""
    return scalar(database, f"SELECT to_regclass('public.{table}') IS NOT NULL") == "t"


def population_sql(suffix: str) -> str:
    """Rows a real deployment would hold: a person with two different roles in two cohorts, a sprint, a ceremony."""
    return f"""
    INSERT INTO users (mattermost_user_id, username, email, is_superadmin)
        VALUES ('verify-mm-{suffix}', 'verify{suffix}', 'verify{suffix}@example.test', false);
    INSERT INTO cohorts (name) VALUES ('Verify-A-{suffix}'), ('Verify-B-{suffix}');
    INSERT INTO cohort_memberships (cohort_id, user_id, role_id)
        SELECT c.id, u.id, r.id FROM cohorts c, users u, roles r
        WHERE c.name = 'Verify-A-{suffix}' AND u.mattermost_user_id = 'verify-mm-{suffix}' AND r.key = 'scrum_master';
    INSERT INTO cohort_memberships (cohort_id, user_id, role_id)
        SELECT c.id, u.id, r.id FROM cohorts c, users u, roles r
        WHERE c.name = 'Verify-B-{suffix}' AND u.mattermost_user_id = 'verify-mm-{suffix}' AND r.key = 'learner';
    INSERT INTO sprints (cohort_id, name, start_date, end_date)
        SELECT id, 'Sprint 1', DATE '2030-01-06', DATE '2030-01-17' FROM cohorts WHERE name = 'Verify-A-{suffix}';
    INSERT INTO ceremonies (cohort_id, sprint_id, ceremony_type_id, organizer_id, scheduled_at, duration_minutes, agenda)
        SELECT c.id, s.id, t.id, u.id, TIMESTAMPTZ '2030-01-06 10:00:00+00', 90, 'Kick-off'
        FROM cohorts c
        JOIN sprints s ON s.cohort_id = c.id
        JOIN ceremony_types t ON t.key = 'sprint_planning'
        JOIN users u ON u.mattermost_user_id = 'verify-mm-{suffix}'
        WHERE c.name = 'Verify-A-{suffix}';
    """


def populate(database: str, suffix: str, label: str) -> None:
    """Insert representative rows into ``database`` so downgrade and re-upgrade run on real data."""
    proc = psql(database, population_sql(suffix))
    check(
        f"{label}: user, 2 cohorts, 2 memberships, sprint, ceremony inserted with psql",
        proc.returncode == 0,
        proc.stderr,
    )
    distinct_roles = scalar(
        database,
        f"SELECT count(DISTINCT role_id) FROM cohort_memberships m JOIN users u ON u.id = m.user_id "
        f"WHERE u.mattermost_user_id = 'verify-mm-{suffix}'",
    )
    check(
        f"{label}: one person holds two different roles in two cohorts (real rows)",
        distinct_roles == "2",
        distinct_roles,
    )
    ceremonies = scalar(
        database, "SELECT count(*) FROM ceremonies WHERE scheduled_at = TIMESTAMPTZ '2030-01-06 10:00:00+00'"
    )
    check(f"{label}: ceremony instant round-trips as timestamptz", ceremonies == "1", ceremonies)
    second_role = psql(
        database,
        f"INSERT INTO cohort_memberships (cohort_id, user_id, role_id) SELECT c.id, u.id, r.id FROM cohorts c, users u, roles r "
        f"WHERE c.name = 'Verify-A-{suffix}' AND u.mattermost_user_id = 'verify-mm-{suffix}' AND r.key = 'learner'",
    )
    check(
        f"{label}: a second role for the same person in the same cohort is rejected by the database",
        second_role.returncode != 0 and "uq_cohort_memberships_user_cohort" in second_role.stderr,
        second_role.stderr or "insert succeeded",
    )


def throwaway_checks(database: str) -> None:
    """Steps 1-5 against the throwaway database."""
    proc = psql(
        database,
        "CREATE TABLE checkpoints (thread_id text NOT NULL, checkpoint_ns text NOT NULL DEFAULT '', "
        "checkpoint_id text NOT NULL, checkpoint jsonb NOT NULL, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)); "
        "INSERT INTO checkpoints VALUES ('verify-thread', '', 'cp-1', '{\"v\": 1}');",
    )
    check("fake pre-existing 'checkpoints' table created with one row", proc.returncode == 0, proc.stderr)

    up = alembic(database, "upgrade", "head")
    check("alembic upgrade head on a fresh database exits 0", up.returncode == 0, up.stderr)
    check(
        f"all {len(DOMAIN_TABLES)} domain tables exist after upgrade",
        count_domain_tables(database) == len(DOMAIN_TABLES),
    )
    version = scalar(database, "SELECT version_num FROM alembic_version")
    check(f"alembic_version is {HEAD_REVISION}", version == HEAD_REVISION, version)
    check("checkpoints row survived the upgrade", scalar(database, "SELECT count(*) FROM checkpoints") == "1")

    again = alembic(database, "upgrade", "head")
    check("second alembic upgrade head exits 0", again.returncode == 0, again.stderr)
    check("second upgrade ran no migration", "Running upgrade" not in again.stdout + again.stderr)

    roles_0 = scalar(database, "SELECT count(*) FROM roles")
    types_0 = scalar(database, "SELECT count(*) FROM ceremony_types")
    check(
        f"migration seeded {EXPECTED_ROLES} roles and {EXPECTED_CEREMONY_TYPES} ceremony types",
        (roles_0, types_0) == (str(EXPECTED_ROLES), str(EXPECTED_CEREMONY_TYPES)),
        f"{roles_0}/{types_0}",
    )
    seed_cmd = ["/app/.venv/bin/python", "/app/scripts/seed_reference_data.py"]
    seed_1 = ai_core(database, seed_cmd)
    check(
        "seed command exits 0 without manual PYTHONPATH",
        seed_1.returncode == 0 and "Reference data seeded" in seed_1.stdout,
        seed_1.stderr,
    )
    roles_1, types_1 = (
        scalar(database, "SELECT count(*) FROM roles"),
        scalar(database, "SELECT count(*) FROM ceremony_types"),
    )
    seed_2 = ai_core(database, seed_cmd)
    check("seed command exits 0 on the second run", seed_2.returncode == 0, seed_2.stderr)
    roles_2, types_2 = (
        scalar(database, "SELECT count(*) FROM roles"),
        scalar(database, "SELECT count(*) FROM ceremony_types"),
    )
    check(
        "role and ceremony-type counts unchanged across migration seed + two seed runs",
        roles_0 == roles_1 == roles_2 and types_0 == types_1 == types_2,
        f"roles {roles_0}->{roles_1}->{roles_2}, types {types_0}->{types_1}->{types_2}",
    )
    keys = scalar(database, "SELECT string_agg(key, ',' ORDER BY key) FROM roles")
    check(
        "seeded role keys are the agreed lowercase machine keys",
        keys == "learner,ops_support,scrum_master,tech_lead",
        keys,
    )
    ctypes = scalar(database, "SELECT string_agg(key, ',' ORDER BY key) FROM ceremony_types")
    check(
        "seeded ceremony types include open_qa",
        ctypes == "daily_standup,open_qa,retrospective,sprint_planning,sprint_review",
        ctypes,
    )

    print("  -- in-container probe (piped over stdin) --")
    with open(PROBE, "rb") as helper:
        probe = ai_core(
            database,
            ["/app/.venv/bin/python", "-"],
            extra_env={"SCHEMA_PROBE_EXPECT_CHECKPOINT_TABLES": "checkpoints", "LOG_LEVEL": "WARNING"},
            stdin=helper,
        )
    probe_lines = [
        line for line in probe.stdout.splitlines() if line.rstrip().endswith(("PASS", "FAIL")) or "  FAIL  (" in line
    ]
    for line in probe_lines:
        results.append(not ("FAIL" in line and not line.rstrip().endswith("PASS")))
        print(f"  probe: {line.strip()}", flush=True)
    check(
        "probe produced assertions", len(probe_lines) >= 50, f"{len(probe_lines)} lines; stderr: {probe.stderr[-300:]}"
    )
    check("probe exit code is 0", probe.returncode == 0, probe.stderr[-300:])

    print("  -- populated downgrade / upgrade cycle --")
    populate(database, "1", "populated")
    down = alembic(database, "downgrade", "base")
    check("alembic downgrade base on the populated database exits 0", down.returncode == 0, down.stderr)
    check(
        "every domain table is gone after downgrade",
        count_domain_tables(database) == 0,
        str(count_domain_tables(database)),
    )
    check("checkpoints table survived the downgrade", table_exists(database, "checkpoints"))
    check("checkpoints row survived the downgrade", scalar(database, "SELECT count(*) FROM checkpoints") == "1")
    stamped = scalar(database, "SELECT count(*) FROM alembic_version")
    check("no revision is stamped after downgrade base", stamped == "0", stamped)
    up_again = alembic(database, "upgrade", "head")
    check("alembic upgrade head after downgrade exits 0", up_again.returncode == 0, up_again.stderr)
    check("domain tables are back after re-upgrade", count_domain_tables(database) == len(DOMAIN_TABLES))
    check(
        "reference data re-seeded by the migration",
        scalar(database, "SELECT count(*) FROM roles") == str(EXPECTED_ROLES),
    )
    populate(database, "2", "re-populated at head")
    drift = alembic(database, "check")
    check(
        "alembic check on the throwaway database: models and migration agree",
        "No new upgrade operations detected" in drift.stdout + drift.stderr,
        (drift.stdout + drift.stderr)[-300:],
    )


def live_checks() -> None:
    """Step 6: the live database, read-only."""
    drift = alembic(LIVE_DB, "check")
    check(
        "alembic check on the LIVE database: no new upgrade operations detected",
        "No new upgrade operations detected" in drift.stdout + drift.stderr,
        (drift.stdout + drift.stderr)[-300:],
    )
    current = alembic(LIVE_DB, "current")
    check(
        f"live database is at {HEAD_REVISION} (head)",
        f"{HEAD_REVISION} (head)" in current.stdout,
        current.stdout + current.stderr,
    )
    for table in CHECKPOINT_TABLES:
        check(f"live checkpointer table '{table}' exists", table_exists(LIVE_DB, table))
    check("live database has every domain table", count_domain_tables(LIVE_DB) == len(DOMAIN_TABLES))
    health = compose(
        [
            "exec",
            "-T",
            "ai-core",
            "python",
            "-c",
            "import urllib.request,sys;r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10);"
            "print(r.status);print(r.read().decode())",
        ]
    )
    status_line, _, body = health.stdout.partition("\n")
    try:
        payload = json.loads(body or "{}")
    except ValueError:
        payload = {}
    check("/health returns 200", status_line.strip() == "200", health.stdout + health.stderr)
    check(
        "/health reports the domain schema healthy",
        payload.get("components", {}).get("domain_schema") == "healthy",
        body[:200],
    )


def smoke_checks() -> None:
    """Step 7: the assistant still answers, and the conversation store is populated."""
    if os.environ.get("SKIP_SMOKE") == "1":
        print("  SKIP  assistant smoke test (SKIP_SMOKE=1)")
        return
    before = scalar(LIVE_DB, "SELECT count(*) FROM checkpoints")
    proc = subprocess.run(["./scripts/smoke_test.sh"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    check(
        "assistant answers after migration (scripts/smoke_test.sh exit 0)",
        proc.returncode == 0,
        (proc.stdout + proc.stderr)[-300:],
    )
    after = scalar(LIVE_DB, "SELECT count(*) FROM checkpoints")
    check(
        "live checkpoints table grew during the conversation",
        before.isdigit() and after.isdigit() and int(after) > int(before),
        f"before={before} after={after}",
    )


def main() -> int:
    """Run every check and return the process exit code."""
    print("=" * 92)
    print("SprintFlow data model — host-side verification")
    print("=" * 92)
    running = compose(["ps", "--services", "--status", "running"]).stdout.split()
    if not check(
        "docker compose reports postgres and ai-core running", {"postgres", "ai-core"} <= set(running), str(running)
    ):
        print("Start the stack first: make up && make bootstrap")
        return 1

    created = False
    try:
        create = compose(["exec", "-T", "postgres", "createdb", "-U", PG_USER, VERIFY_DB])
        created = create.returncode == 0
        if not check(f"throwaway database {VERIFY_DB} created", created, create.stderr):
            return 1
        throwaway_checks(VERIFY_DB)
        live_checks()
        smoke_checks()
    finally:
        if created:
            drop = compose(["exec", "-T", "postgres", "dropdb", "--if-exists", "--force", "-U", PG_USER, VERIFY_DB])
            check(f"throwaway database {VERIFY_DB} dropped", drop.returncode == 0, drop.stderr)
    print("=" * 92)
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    print("SCHEMA VERIFICATION OK" if all(results) else "SCHEMA VERIFICATION FAILED")
    return 0 if all(results) else 1


load_dotenv()
PG_USER = os.environ.get("POSTGRES_USER", "sprintflow")
LIVE_DB = os.environ.get("POSTGRES_DB", "sprintflow")
VERIFY_DB = f"sprintflow_verify_{time.strftime('%Y%m%d%H%M%S')}"

if __name__ == "__main__":
    sys.exit(main())
