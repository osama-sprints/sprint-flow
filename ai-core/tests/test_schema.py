"""Pure-logic tests for the SprintFlow data model (no database, no network).

They pin the contracts other tasks build on: the model import surface, the
migration's table list and seed snapshot, naming, timezone-awareness, the
machine keys and their aliases, and the externally owned tables Alembic must
never touch. Database behaviour is covered by ``tests/integration`` and by
``scripts/verify_schema.py``.
"""

import importlib.util
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    UniqueConstraint,
)
from sqlmodel import SQLModel

import app.models  # noqa: F401  (populates the metadata)
from app.models import (
    DOMAIN_TABLES,
    Ceremony,
    require_aware,
)
from app.models.enums import (
    CEREMONY_TYPE_DEFAULT_DURATION_MINUTES,
    CEREMONY_TYPE_LABELS,
    CHANNEL_ADMIN_ROLES,
    ROLE_LABELS,
    CeremonyTypeKey,
    RoleKey,
    normalise_ceremony_type_key,
    normalise_role_key,
)
from app.services.database import (
    EXTERNALLY_OWNED_TABLES,
    is_externally_owned,
)
from app.services.domain.ceremonies import AMENDABLE_FIELDS
from app.services.domain.escalations import format_ticket_ref

MIGRATION_FILE = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_sprintflow_domain_schema.py"


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0001", MIGRATION_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOp:
    """Stands in for ``alembic.op`` so ``downgrade()`` can be run without a database."""

    def __init__(self) -> None:
        self.dropped: list[str] = []

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)


# ---------------------------------------------------------------------------
# Import surface and table list
# ---------------------------------------------------------------------------


def test_domain_tables_match_model_metadata_exactly():
    assert set(SQLModel.metadata.tables) == set(DOMAIN_TABLES)
    assert len(DOMAIN_TABLES) == len(set(DOMAIN_TABLES))


def test_domain_tables_are_in_dependency_order():
    seen: set[str] = set()
    for name in DOMAIN_TABLES:
        for fk in SQLModel.metadata.tables[name].foreign_key_constraints:
            target = fk.referred_table.name
            assert target == name or target in seen, f"{name} references {target} before it is created"
        seen.add(name)


def test_explicit_plural_snake_case_table_names():
    for name in DOMAIN_TABLES:
        assert name == name.lower(), f"{name} is not lowercase"
        assert " " not in name and "-" not in name, f"{name} is not snake_case"
        assert name.endswith("s"), f"{name} is not plural"


def test_every_domain_table_carries_audit_columns():
    for name in DOMAIN_TABLES:
        columns = SQLModel.metadata.tables[name].columns
        assert "created_at" in columns and "updated_at" in columns, name


# ---------------------------------------------------------------------------
# Migration agrees with the models
# ---------------------------------------------------------------------------


def test_migration_downgrade_drops_exactly_the_domain_tables_in_reverse_order():
    migration = load_migration()
    op = RecordingOp()
    migration.op = op
    migration.downgrade()
    assert op.dropped == list(reversed(DOMAIN_TABLES))


def test_migration_head_revision_id():
    migration = load_migration()
    assert migration.revision == "0001_sprintflow_domain"
    assert migration.down_revision is None


def test_migration_seed_snapshot_matches_the_enums():
    migration = load_migration()
    assert [key for key, _, _ in migration.ROLE_SEED] == [r.value for r in RoleKey]
    assert {key: label for key, label, _ in migration.ROLE_SEED} == {r.value: ROLE_LABELS[r] for r in RoleKey}
    assert [key for key, _, _ in migration.CEREMONY_TYPE_SEED] == [c.value for c in CeremonyTypeKey]
    assert {key: minutes for key, _, minutes in migration.CEREMONY_TYPE_SEED} == {
        c.value: CEREMONY_TYPE_DEFAULT_DURATION_MINUTES[c] for c in CeremonyTypeKey
    }
    assert {key: label for key, label, _ in migration.CEREMONY_TYPE_SEED} == {
        c.value: CEREMONY_TYPE_LABELS[c] for c in CeremonyTypeKey
    }


def test_migration_legacy_tables_are_neither_domain_nor_external():
    migration = load_migration()
    legacy = set(migration.LEGACY_TEMPLATE_TABLES)
    assert not legacy & set(DOMAIN_TABLES)
    assert not legacy & EXTERNALLY_OWNED_TABLES


# ---------------------------------------------------------------------------
# Constraints, names and timezone policy declared by the models
# ---------------------------------------------------------------------------


def test_every_datetime_column_is_timezone_aware():
    naive = [
        f"{table}.{column.name}"
        for table in DOMAIN_TABLES
        for column in SQLModel.metadata.tables[table].columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert naive == []


def test_unique_and_check_constraints_are_named():
    for table in DOMAIN_TABLES:
        for constraint in SQLModel.metadata.tables[table].constraints:
            if isinstance(constraint, (UniqueConstraint, CheckConstraint)):
                assert constraint.name, f"{table}: unnamed {type(constraint).__name__}"
                assert str(constraint.name).startswith(("uq_", "ck_")), constraint.name


def test_one_role_per_person_per_channel_is_a_database_constraint():
    membership = SQLModel.metadata.tables["channel_roles"]
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in membership.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("user_id", "channel_id") in uniques
    assert "role_id" not in SQLModel.metadata.tables["users"].columns, "a global role on users is forbidden"


def test_onboarding_outbox_key_treats_null_channel_as_a_value():
    steps = SQLModel.metadata.tables["onboarding_steps"]
    key = next(c for c in steps.constraints if isinstance(c, UniqueConstraint))
    assert tuple(c.name for c in key.columns) == ("user_id", "channel_id", "step_kind")
    assert key.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_escalation_ticket_carries_both_conversations():
    columns = SQLModel.metadata.tables["escalation_tickets"].columns
    for name in (
        "ticket_ref",
        "learner_id",
        "assigned_human_id",
        "status",
        "status_changed_at",
        "question",
        "channel_id",
        "learner_thread_id",
        "human_dm_channel_id",
        "human_dm_thread_id",
    ):
        assert name in columns, name


def test_require_aware_rejects_naive_and_returns_aware_unchanged():
    aware = datetime(2030, 1, 1, 9, tzinfo=UTC)
    assert require_aware(aware, "x") is aware
    with pytest.raises(ValueError, match="scheduled_at must be timezone-aware"):
        require_aware(datetime(2030, 1, 1, 9), "scheduled_at")


# ---------------------------------------------------------------------------
# Machine keys and aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Tech Lead", RoleKey.TECH_LEAD),
        ("tech-lead", RoleKey.TECH_LEAD),
        ("SCRUM_MASTER", RoleKey.SCRUM_MASTER),
        ("scrum master", RoleKey.SCRUM_MASTER),
        ("ops", RoleKey.OPS_SUPPORT),
        ("student", RoleKey.LEARNER),
        ("chief", None),
    ],
)
def test_role_aliases(typed: str, expected: RoleKey | None):
    assert normalise_role_key(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("standup", CeremonyTypeKey.DAILY_STANDUP),
        ("Stand-up", CeremonyTypeKey.DAILY_STANDUP),
        ("retro", CeremonyTypeKey.RETROSPECTIVE),
        ("demo", CeremonyTypeKey.SPRINT_REVIEW),
        ("Q&A", CeremonyTypeKey.OPEN_QA),
        ("office hours", CeremonyTypeKey.OPEN_QA),
        ("planning", CeremonyTypeKey.SPRINT_PLANNING),
        ("lunch", None),
    ],
)
def test_ceremony_type_aliases(typed: str, expected: CeremonyTypeKey | None):
    assert normalise_ceremony_type_key(typed) == expected


def test_machine_keys_are_lowercase_and_labelled():
    for key in RoleKey:
        assert key.value == key.value.lower() and key in ROLE_LABELS
    for key in CeremonyTypeKey:
        assert key.value == key.value.lower() and key in CEREMONY_TYPE_LABELS
    assert CEREMONY_TYPE_LABELS[CeremonyTypeKey.OPEN_QA] == "Open Q&A"


def test_only_tech_lead_and_scrum_master_administer_a_channel():
    assert CHANNEL_ADMIN_ROLES == {RoleKey.TECH_LEAD, RoleKey.SCRUM_MASTER}
    assert RoleKey.LEARNER not in CHANNEL_ADMIN_ROLES


# ---------------------------------------------------------------------------
# Data-access layer contracts
# ---------------------------------------------------------------------------


def test_ticket_ref_format():
    assert format_ticket_ref(1) == "ESC-000001"
    assert format_ticket_ref(42) == "ESC-000042"
    assert format_ticket_ref(1234567) == "ESC-1234567"



def test_externally_owned_tables_cover_the_checkpointer_and_nothing_of_ours():
    assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"} <= EXTERNALLY_OWNED_TABLES
    assert not EXTERNALLY_OWNED_TABLES & set(DOMAIN_TABLES)
    assert is_externally_owned("checkpoints")
    assert not is_externally_owned("channels")
    assert not is_externally_owned(None)
