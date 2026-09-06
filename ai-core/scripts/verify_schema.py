"""In-container schema probe: prints one PASS/FAIL line per assertion, exits non-zero on failure.

Driven by the host-side ``scripts/verify_schema.py`` (which pipes this file
over stdin into the ai-core container so the copy under test is the one in the
working tree, never a stale image layer), or run directly:

    docker compose exec -T ai-core /app/.venv/bin/python - < ai-core/scripts/verify_schema.py
    docker compose exec -T ai-core /app/.venv/bin/python /app/scripts/verify_schema.py

or on a developer machine against a throwaway database (see docs/database.md):

    APP_ENV=test POSTGRES_DB=<throwaway> ... .venv/bin/python scripts/verify_schema.py

It compares the live database with the model metadata (every table, every
column, every named constraint, timestamptz everywhere, NULLS NOT DISTINCT on
the onboarding outbox key), confirms the externally owned checkpoint tables
are present, then proves the behavioural invariants with real rows through
the data-access layer — one person with two roles in two cohorts, idempotent
upserts, touching ceremonies not conflicting, unique ticket references — and
removes every row it created, even when an assertion fails.

Environment:
    SCHEMA_PROBE_EXPECT_CHECKPOINT_TABLES  Comma-separated checkpointer tables
        that must exist. Defaults to all four LangGraph tables (the live
        database). The host verifier passes ``checkpoints`` for its throwaway
        database, where it pre-creates only that one.
"""

import asyncio
import os
import sys
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from pathlib import Path
from typing import Any

# ``__file__`` is undefined when the source arrives over stdin (``python -``);
# the container's working directory (/app) is then already on sys.path.
_this_file = globals().get("__file__")
if _this_file:
    sys.path.insert(0, str(Path(_this_file).resolve().parents[1]))
sys.path.insert(0, "/app")

from sqlalchemy import (  # noqa: E402
    DateTime,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.models import DOMAIN_TABLES  # noqa: E402
from app.models.enums import (  # noqa: E402
    CeremonyStatus,
    CeremonyTypeKey,
    EscalationStatus,
    EscalationType,
    MembershipStatus,
    OnboardingStepKind,
    OnboardingStepStatus,
    RoleKey,
)
from app.services.database import (  # noqa: E402
    EXTERNALLY_OWNED_TABLES,
    database_service,
    database_url,
)
from app.services.domain import ceremonies as ceremony_repo  # noqa: E402
from app.services.domain import cohorts as cohort_repo  # noqa: E402
from app.services.domain import escalations as escalation_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402
from app.services.domain import onboarding as onboarding_repo  # noqa: E402
from app.services.domain import sprints as sprint_repo  # noqa: E402
from app.services.domain import standups as standup_repo  # noqa: E402
from app.services.domain.reference_data import seed_reference_data  # noqa: E402

HEAD_REVISION = "0002_sprints_name_ci"
DEFAULT_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")
LEGACY_TABLES = (
    "user",
    "session",
    "thread",
    "person",
    "cohort",
    "role",
    "ceremonytype",
    "cohortmembership",
    "sprint",
    "ceremony",
    "dailyprogress",
    "escalation",
    "onboarding_state",
)
NAMED_CHECKS = {
    "sprints": {"ck_sprints_dates_ordered"},
    "ceremonies": {"ck_ceremonies_duration_positive"},
}
UNIQUE_INDEXES = {
    "users": {"ix_users_mattermost_user_id"},
    "roles": {"ix_roles_key"},
    "ceremony_types": {"ix_ceremony_types_key"},
    "cohorts": {"ux_cohorts_name_lower"},
    "sprints": {"ux_sprints_cohort_name_lower"},
    "escalation_tickets": {"ix_escalation_tickets_ticket_ref"},
}

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append(ok)
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {label:70} {'PASS' if ok else 'FAIL'}{suffix}", flush=True)


def expected_checkpoint_tables() -> tuple[str, ...]:
    """Read which checkpointer tables this database is expected to hold."""
    raw = os.getenv("SCHEMA_PROBE_EXPECT_CHECKPOINT_TABLES")
    if raw is None:
        return DEFAULT_CHECKPOINT_TABLES
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def structural_checks(engine: Engine) -> None:
    """Compare the live schema with what the models and the migration declare."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    for table in DOMAIN_TABLES:
        check(f"table '{table}' exists", table in tables)
    check(
        "model import surface covers every domain table",
        set(SQLModel.metadata.tables) == set(DOMAIN_TABLES),
        f"metadata={sorted(SQLModel.metadata.tables)}",
    )
    for table in expected_checkpoint_tables():
        check(f"externally owned table '{table}' still present", table in tables)
    check(
        "no externally owned table is a domain table",
        not (EXTERNALLY_OWNED_TABLES & set(DOMAIN_TABLES)),
    )
    for legacy in LEGACY_TABLES:
        check(f"legacy table '{legacy}' is absent", legacy not in tables)

    for table in DOMAIN_TABLES:
        if table not in tables:
            check(f"{table}: columns match the model", False, "table missing")
            continue
        model_columns = set(SQLModel.metadata.tables[table].columns.keys())
        db_columns = {column["name"] for column in inspector.get_columns(table)}
        check(
            f"{table}: columns match the model exactly",
            model_columns == db_columns,
            f"model-only={sorted(model_columns - db_columns)} db-only={sorted(db_columns - model_columns)}",
        )

    for table in DOMAIN_TABLES:
        if table not in tables:
            continue
        naive = [
            column["name"]
            for column in inspector.get_columns(table)
            if isinstance(column["type"], DateTime) and not getattr(column["type"], "timezone", False)
        ]
        check(f"{table}: every timestamp column is timestamptz", not naive, f"naive={naive}")

    for table in DOMAIN_TABLES:
        if table not in tables:
            continue
        model_table = SQLModel.metadata.tables[table]
        wanted_uniques = {
            tuple(c.name for c in constraint.columns)
            for constraint in model_table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        declared = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
        declared |= {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i.get("unique")}
        for wanted in sorted(wanted_uniques):
            check(f"{table}: unique {wanted}", wanted in declared)
        unnamed = [u for u in inspector.get_unique_constraints(table) if not u.get("name")]
        check(f"{table}: unique constraints are named", not unnamed)

    for table in DOMAIN_TABLES:
        if table not in tables:
            continue
        model_fks = {
            (tuple(fk.column_keys), fk.referred_table.name)
            for fk in SQLModel.metadata.tables[table].foreign_key_constraints
        }
        db_fks = inspector.get_foreign_keys(table)
        db_fk_shapes = {(tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in db_fks}
        check(f"{table}: foreign keys match the model", model_fks == db_fk_shapes, f"db={sorted(db_fk_shapes)}")
        unnamed_fks = [fk for fk in db_fks if not (fk.get("name") or "").startswith("fk_")]
        check(f"{table}: foreign keys are explicitly named (fk_*)", not unnamed_fks, str(unnamed_fks)[:120])

    for table, names in NAMED_CHECKS.items():
        if table not in tables:
            continue
        present = {c["name"] for c in inspector.get_check_constraints(table)}
        check(f"{table}: check constraints {sorted(names)}", names <= present, f"have {sorted(present)}")

    for table, names in UNIQUE_INDEXES.items():
        if table not in tables:
            continue
        present = {i["name"] for i in inspector.get_indexes(table) if i.get("unique")}
        check(f"{table}: unique indexes {sorted(names)}", names <= present, f"have {sorted(present)}")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        check(f"alembic_version is at {HEAD_REVISION}", version == HEAD_REVISION, str(version))
        nulls_distinct = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'uq_onboarding_steps_user_cohort_kind'"
            )
        ).scalar()
        check(
            "onboarding_steps unique key treats NULL cohort as one value (NULLS NOT DISTINCT)",
            bool(nulls_distinct) and "NULLS NOT DISTINCT" in str(nulls_distinct),
            str(nulls_distinct),
        )
        cohort_index = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_cohorts_name_lower'")
        ).scalar()
        check(
            "cohort names are unique case-insensitively (functional index on lower(name))",
            bool(cohort_index) and "lower(" in str(cohort_index) and "UNIQUE" in str(cohort_index),
            str(cohort_index),
        )
        pk_names = (
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE contype = 'p' AND conrelid::regclass::text = ANY(:tables)"
                ),
                {"tables": list(DOMAIN_TABLES)},
            )
            .scalars()
            .all()
        )
        check(
            "primary keys are explicitly named (pk_*)",
            len(pk_names) == len(DOMAIN_TABLES) and all(str(n).startswith("pk_") for n in pk_names),
            str(sorted(str(n) for n in pk_names)),
        )


async def behavioural_checks(suffix: str, created: dict[str, list[int]]) -> None:
    """Prove the invariants with real rows through the data-access layer."""
    first = await seed_reference_data()
    roles_before = await cohort_repo.list_roles()
    types_before = await ceremony_repo.list_ceremony_types()
    await seed_reference_data()
    roles_after = await cohort_repo.list_roles()
    types_after = await ceremony_repo.list_ceremony_types()
    check(
        "roles seeded with the agreed keys",
        {r.key for r in roles_after} == set(RoleKey),
        f"have {sorted(r.key for r in roles_after)}",
    )
    check(
        "ceremony types seeded (including open Q&A)",
        {t.key for t in types_after} == set(CeremonyTypeKey),
        f"have {sorted(t.key for t in types_after)}",
    )
    check(
        "seeding twice changes no row count",
        len(roles_before) == len(roles_after) == first.roles
        and len(types_before) == len(types_after) == first.ceremony_types,
        f"{len(roles_before)}->{len(roles_after)}, {len(types_before)}->{len(types_after)}",
    )

    # --- identity ---------------------------------------------------------
    mm_id = f"probe-{suffix}"
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mm_id,
        username=f"probe{suffix}",
        email=f"probe{suffix}@example.test",
        display_name="Probe",
        timezone="Europe/Berlin",
        is_superadmin=False,
    )
    assert user.id is not None
    created["users"].append(user.id)
    again = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mm_id,
        username=f"probe{suffix}-renamed",
        email=f"probe{suffix}@example.test",
        display_name="Probe",
        timezone="Europe/Berlin",
        is_superadmin=True,
    )
    check(
        "identity upsert is idempotent (same row, refreshed handle and superadmin flag)",
        user.id == again.id and again.username.endswith("renamed") and again.is_superadmin,
    )
    by_mm = await identity_repo.get_user_by_mattermost_id(mm_id)
    by_handle = await identity_repo.get_user_by_username(f"@PROBE{suffix}-RENAMED")
    by_email = await identity_repo.get_user_by_email(f"PROBE{suffix}@EXAMPLE.TEST")
    check(
        "a chat identity resolves by Mattermost id, @handle and email to one record",
        all(u is not None and u.id == user.id for u in (by_mm, by_handle, by_email)),
    )
    check("stored profile timezone survives the round trip", again.timezone == "Europe/Berlin")

    # --- cohorts, roles, memberships ---------------------------------------
    cohort_a = await cohort_repo.create_cohort(f"Probe-A-{suffix}", created_by_id=user.id)
    cohort_b = await cohort_repo.create_cohort(f"Probe-B-{suffix}")
    assert cohort_a.id and cohort_b.id
    created["cohorts"].extend([cohort_a.id, cohort_b.id])
    learner = await cohort_repo.get_role_by_key(RoleKey.LEARNER)
    lead = await cohort_repo.get_role_by_key(RoleKey.TECH_LEAD)
    scrum = await cohort_repo.get_role_by_key(RoleKey.SCRUM_MASTER)
    assert learner and learner.id and lead and lead.id and scrum and scrum.id

    first_assignment = await cohort_repo.upsert_membership(
        user_id=user.id, cohort_id=cohort_a.id, role_id=learner.id, assigned_by_id=user.id
    )
    await cohort_repo.upsert_membership(user_id=user.id, cohort_id=cohort_b.id, role_id=lead.id, assigned_by_id=None)
    role_a = await cohort_repo.get_role_for_user_in_cohort(user.id, cohort_a.id)
    role_b = await cohort_repo.get_role_for_user_in_cohort(user.id, cohort_b.id)
    check(
        "one person holds different roles in two cohorts (learner in A, tech_lead in B)",
        bool(role_a and role_b) and role_a.key == RoleKey.LEARNER and role_b.key == RoleKey.TECH_LEAD,
    )
    check("first assignment reports created=True", first_assignment.created)
    repeat = await cohort_repo.upsert_membership(
        user_id=user.id, cohort_id=cohort_a.id, role_id=learner.id, assigned_by_id=None
    )
    check(
        "repeating a role assignment creates no duplicate and reports the same role",
        not repeat.created
        and repeat.previous_role_id == learner.id
        and repeat.membership.id == first_assignment.membership.id,
    )
    changed = await cohort_repo.upsert_membership(
        user_id=user.id, cohort_id=cohort_a.id, role_id=scrum.id, assigned_by_id=None
    )
    members_a = await cohort_repo.list_cohort_members(cohort_a.id)
    check(
        "changing the role in a cohort updates the one row and names the previous role",
        changed.previous_role_id == learner.id
        and changed.membership.id == first_assignment.membership.id
        and len([m for m in members_a if m.user.id == user.id]) == 1
        and members_a[0].role.key == RoleKey.SCRUM_MASTER,
    )
    memberships = await cohort_repo.list_memberships_for_user(user.id)
    check(
        "membership lookup returns exactly the two cohorts",
        {c.id for _, c, _ in memberships} == {cohort_a.id, cohort_b.id},
    )
    stranger_role = await cohort_repo.get_role_for_user_in_cohort(user.id, 0)
    check("role lookup in a cohort the person is not in is None (no authority leaks)", stranger_role is None)
    await cohort_repo.set_membership_status(user.id, cohort_b.id, MembershipStatus.INACTIVE)
    inactive_role = await cohort_repo.get_role_for_user_in_cohort(user.id, cohort_b.id)
    check("an inactive membership confers no role by default", inactive_role is None)
    by_id = await cohort_repo.resolve_cohort(f"#{cohort_a.id}")
    by_name = await cohort_repo.resolve_cohort(cohort_a.name.upper())
    check(
        "cohort resolves by numeric id and by case-insensitive name",
        bool(by_id and by_name) and by_id.id == by_name.id == cohort_a.id,
    )

    # --- kill switch ---------------------------------------------------------
    await cohort_repo.set_cohort_active(cohort_b.id, False)
    active_ids = {c.id for c in await cohort_repo.list_cohorts(active_only=True)}
    all_ids = {c.id for c in await cohort_repo.list_cohorts()}
    switched = await cohort_repo.get_cohort(cohort_b.id)
    check(
        "cohort kill switch: inactive cohort disappears from active listings and stamps deactivated_at",
        cohort_b.id not in active_ids and cohort_b.id in all_ids and bool(switched and switched.deactivated_at),
    )

    # --- sprints ---------------------------------------------------------------
    sprint = await sprint_repo.create_sprint(
        cohort_id=cohort_a.id,
        name="Sprint 1",
        start_date=date(2030, 1, 7),
        end_date=date(2030, 1, 18),
        opened_by_id=user.id,
    )
    assert sprint.id
    found_sprint = await sprint_repo.get_sprint_by_name(cohort_a.id, " sprint 1 ")
    adjacent = await sprint_repo.find_overlapping_sprints(cohort_a.id, date(2030, 1, 19), date(2030, 1, 30))
    overlapping = await sprint_repo.find_overlapping_sprints(cohort_a.id, date(2030, 1, 18), date(2030, 1, 30))
    check(
        "sprint resolves by cohort + name case/space-insensitively",
        bool(found_sprint and found_sprint.id == sprint.id),
    )
    check(
        "sprint overlap: sharing one day overlaps, starting the next day does not",
        [s.id for s in overlapping] == [sprint.id] and not adjacent,
    )

    # --- ceremonies ------------------------------------------------------------
    standup_type = await ceremony_repo.get_ceremony_type_by_key(CeremonyTypeKey.DAILY_STANDUP)
    assert standup_type and standup_type.id
    start = datetime(2030, 1, 7, 10, 0, tzinfo=UTC)
    ceremony = await ceremony_repo.create_ceremony(
        cohort_id=cohort_a.id,
        ceremony_type_id=standup_type.id,
        organizer_id=user.id,
        scheduled_at=start,
        duration_minutes=30,
        sprint_id=sprint.id,
        agenda="Probe standup",
        time_expression="10am",
        time_zone="UTC",
    )
    assert ceremony.id
    stored = await ceremony_repo.get_ceremony(ceremony.id)
    check(
        "ceremony instant is stored timezone-aware and equal to the given instant",
        bool(stored and stored.scheduled_at.tzinfo is not None and stored.scheduled_at == start),
    )
    naive_rejected = False
    try:
        await ceremony_repo.create_ceremony(
            cohort_id=cohort_a.id,
            ceremony_type_id=standup_type.id,
            organizer_id=user.id,
            scheduled_at=datetime(2030, 1, 7, 11, 0),
            duration_minutes=30,
        )
    except ValueError:
        naive_rejected = True
    check("a naive ceremony instant is rejected before it reaches the database", naive_rejected)
    touching = await ceremony_repo.find_overlapping_ceremonies(cohort_a.id, start + timedelta(minutes=30), 30)
    inside = await ceremony_repo.find_overlapping_ceremonies(cohort_a.id, start + timedelta(minutes=29), 30)
    check("touching ceremonies (10:00-10:30, 10:30-11:00) do not conflict", not touching)
    check("overlapping ceremonies (10:00-10:30, 10:29-10:59) do conflict", [c.id for c in inside] == [ceremony.id])
    amended = await ceremony_repo.update_ceremony(
        ceremony.id,
        amended_by_id=user.id,
        changes={"agenda": "Probe standup (amended)", "duration_minutes": 30},
        reason="probe",
    )
    trail = await ceremony_repo.list_amendments(ceremony.id)
    check(
        "amending a ceremony writes one audit row per changed field only",
        bool(amended and amended.agenda.endswith("(amended)")) and [a.field for a in trail] == ["agenda"],
        str([a.field for a in trail]),
    )
    unknown_rejected = False
    try:
        await ceremony_repo.update_ceremony(ceremony.id, amended_by_id=user.id, changes={"cohort_id": cohort_b.id})
    except ValueError:
        unknown_rejected = True
    check("amending a non-amendable field raises ValueError", unknown_rejected)
    await ceremony_repo.update_ceremony(
        ceremony.id, amended_by_id=user.id, changes={"status": CeremonyStatus.CANCELLED}
    )
    upcoming = await ceremony_repo.list_ceremonies(cohort_a.id, now=start - timedelta(days=1))
    with_cancelled = await ceremony_repo.list_ceremonies(
        cohort_a.id, include_cancelled=True, now=start - timedelta(days=1)
    )
    no_conflict = await ceremony_repo.find_overlapping_ceremonies(cohort_a.id, start, 30)
    check(
        "a cancelled ceremony leaves the calendar and never conflicts",
        not upcoming and [c.id for c in with_cancelled] == [ceremony.id] and not no_conflict,
    )

    # --- standups --------------------------------------------------------------
    await standup_repo.upsert_daily_standup(
        sprint_id=sprint.id, learner_id=user.id, log_date=date(2030, 1, 8), what_i_did="a", what_i_will_do="b"
    )
    await standup_repo.upsert_daily_standup(
        sprint_id=sprint.id,
        learner_id=user.id,
        log_date=date(2030, 1, 8),
        what_i_did="a2",
        what_i_will_do="b2",
        blockers="c",
    )
    entries = await standup_repo.list_daily_standups(sprint.id, learner_id=user.id)
    check(
        "one standup entry per person per day; a repeat overwrites it",
        len(entries) == 1 and entries[0].what_i_did == "a2" and entries[0].blockers == "c",
    )

    # --- escalations -----------------------------------------------------------
    ticket = await escalation_repo.create_escalation_ticket(
        cohort_id=cohort_a.id,
        learner_id=user.id,
        ticket_type=EscalationType.OPS,
        question="Can I skip Friday's standup?",
        learner_channel_id="chan-probe",
        learner_thread_id=f"thread-{suffix}",
        sprint_id=sprint.id,
    )
    check(
        "escalation ticket gets an ESC-000001-style reference",
        ticket.ticket_ref.startswith("ESC-") and len(ticket.ticket_ref) == 10,
    )
    routed = await escalation_repo.set_escalation_status(
        ticket.ticket_ref,
        EscalationStatus.WAITING_HUMAN,
        assigned_human_id=user.id,
        human_dm_channel_id="dm-probe",
        human_dm_thread_id=f"dm-thread-{suffix}",
    )
    correlated = await escalation_repo.get_escalation_ticket_by_human_thread(f"dm-thread-{suffix}")
    check(
        "ticket carries both conversations and correlates back from the human DM thread",
        bool(routed and correlated)
        and correlated.ticket_ref == ticket.ticket_ref
        and correlated.learner_thread_id == f"thread-{suffix}"
        and correlated.status == EscalationStatus.WAITING_HUMAN
        and correlated.status_changed_at > ticket.status_changed_at,
    )
    resolved = await escalation_repo.set_escalation_status(ticket.ticket_ref, EscalationStatus.RESOLVED, answer="Yes")
    check(
        "resolving stamps resolved_at and stores the answer",
        bool(resolved and resolved.resolved_at and resolved.answer == "Yes"),
    )
    open_in_a = await escalation_repo.list_escalation_tickets(cohort_a.id, status=EscalationStatus.OPEN)
    check("no ticket is still open in the cohort (query by cohort + status)", not open_in_a)

    # --- onboarding outbox -----------------------------------------------------
    due = datetime.now(UTC) - timedelta(minutes=1)
    step, made = await onboarding_repo.enqueue_step(
        user_id=user.id, cohort_id=None, step_kind=OnboardingStepKind.WELCOME, due_at=due
    )
    step2, made2 = await onboarding_repo.enqueue_step(
        user_id=user.id, cohort_id=None, step_kind=OnboardingStepKind.WELCOME, due_at=due
    )
    check("enqueueing the same onboarding step twice yields one row", made and not made2 and step.id == step2.id)
    naive_due_rejected = False
    try:
        await onboarding_repo.enqueue_step(
            user_id=user.id, cohort_id=None, step_kind=OnboardingStepKind.FOLLOW_UP, due_at=datetime(2030, 1, 1, 9)
        )
    except ValueError:
        naive_due_rejected = True
    check("a naive onboarding due_at is rejected", naive_due_rejected)
    await onboarding_repo.enqueue_step(
        user_id=user.id, cohort_id=cohort_b.id, step_kind=OnboardingStepKind.ORIENTATION, due_at=due
    )
    claimed = await onboarding_repo.claim_due_steps(worker_id=f"probe-{suffix}", lease_seconds=120, limit=50)
    claimed_ids = {s.id for s in claimed}
    check("claiming due steps takes the pending rows for this person", {step.id} <= claimed_ids)
    reclaimed = await onboarding_repo.claim_due_steps(worker_id="probe-other", lease_seconds=120, limit=50)
    check("a second worker cannot claim rows under a live lease", not ({step.id} & {s.id for s in reclaimed}))
    halted = await onboarding_repo.halt_steps_for_cohort(cohort_b.id)
    halted_rows = [s for s in await onboarding_repo.list_steps_for_user(user.id) if s.cohort_id == cohort_b.id]
    check(
        "deactivating a cohort halts its pending onboarding steps",
        halted == 1 and all(s.status == OnboardingStepStatus.HALTED for s in halted_rows),
    )


def cleanup(engine: Engine, suffix: str, created: dict[str, list[int]]) -> None:
    """Remove every probe row in dependency order. Runs even when checks failed."""
    users = created["users"] or [-1]
    cohorts = created["cohorts"] or [-1]
    with engine.begin() as conn:
        params: dict[str, Any] = {"users": users, "cohorts": cohorts}
        conn.execute(text("DELETE FROM onboarding_steps WHERE user_id = ANY(:users)"), params)
        conn.execute(text("DELETE FROM escalation_tickets WHERE cohort_id = ANY(:cohorts)"), params)
        conn.execute(text("DELETE FROM daily_standups WHERE learner_id = ANY(:users)"), params)
        conn.execute(
            text(
                "DELETE FROM ceremony_amendments WHERE ceremony_id IN "
                "(SELECT id FROM ceremonies WHERE cohort_id = ANY(:cohorts))"
            ),
            params,
        )
        conn.execute(text("DELETE FROM ceremonies WHERE cohort_id = ANY(:cohorts)"), params)
        conn.execute(text("DELETE FROM sprints WHERE cohort_id = ANY(:cohorts)"), params)
        conn.execute(text("DELETE FROM cohort_memberships WHERE cohort_id = ANY(:cohorts)"), params)
        conn.execute(text("DELETE FROM cohorts WHERE id = ANY(:cohorts)"), params)
        conn.execute(
            text("DELETE FROM users WHERE id = ANY(:users) OR mattermost_user_id = :mm"),
            {**params, "mm": f"probe-{suffix}"},
        )
        leftovers = conn.execute(text("SELECT count(*) FROM users WHERE mattermost_user_id LIKE 'probe-%'")).scalar()
    check("probe rows removed (no probe users left behind)", leftovers == 0, str(leftovers))


async def run_behavioural(engine: Engine) -> None:
    """Run the behavioural checks with guaranteed cleanup."""
    suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    created: dict[str, list[int]] = {"users": [], "cohorts": []}
    try:
        await behavioural_checks(suffix, created)
    except Exception as exc:  # noqa: BLE001 - report, then still clean up
        check(f"behavioural checks completed without exception ({type(exc).__name__}: {exc})", False)
    finally:
        await database_service.close()
        cleanup(engine, suffix, created)


def main() -> int:
    """Run every check.

    Returns:
        int: 0 when all pass.
    """
    print("=" * 84)
    print(f"SprintFlow domain schema — in-container probe (database: {database_url().rsplit('/', 1)[-1]})")
    print("=" * 84)
    engine = create_engine(database_url(), poolclass=NullPool)
    try:
        structural_checks(engine)
        asyncio.run(run_behavioural(engine))
    finally:
        engine.dispose()
    print("=" * 84)
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    print("SCHEMA VERIFICATION OK" if all(results) else "SCHEMA VERIFICATION FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
