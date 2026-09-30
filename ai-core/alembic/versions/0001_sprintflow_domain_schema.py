"""Create the SprintFlow domain schema.

Revision ID: 0001_sprintflow_domain
Revises:
Create Date: 2026-09-03

This is the consolidated Sprint 1 migration. It replaces the six pre-merge
branch revisions (which could not be downgraded and could not upgrade a
populated database). Environments that applied those branch revisions are
development-only; reset them with ``make db-reset`` before upgrading.

The LangGraph checkpointer tables (``checkpoints``, ``checkpoint_blobs``,
``checkpoint_writes``, ``checkpoint_migrations``) are never referenced here and
are excluded from every comparison in ``env.py``.

Reference data (roles, ceremony types) is seeded in the same revision that
creates the lookup tables, idempotently, so a blank database reaches a usable
state with one command. ``app.services.domain.reference_data`` re-seeds at
startup with the same keys.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_sprintflow_domain"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.DateTime(timezone=True)

# Snapshot of the seed keys at the time of this revision. The service-level
# seeder is the living source of truth; this snapshot guarantees a fresh
# database is usable the moment the migration finishes.
ROLE_SEED = (
    ("learner", "Learner", "Takes part in the cohort's sprints, standups and ceremonies."),
    ("tech_lead", "Tech Lead", "Resolves technical escalations and may administer the cohort."),
    ("ops_support", "Ops Support", "Resolves policy and operational escalations for the cohort."),
    ("scrum_master", "Scrum Master", "Runs the agile ceremonies and may administer the cohort."),
)
CEREMONY_TYPE_SEED = (
    ("daily_standup", "Daily Standup", 15),
    ("sprint_planning", "Sprint Planning", 90),
    ("sprint_review", "Sprint Review", 60),
    ("retrospective", "Retrospective", 60),
    ("open_qa", "Open Q&A", 60),
)

# Tables created by the upstream FastAPI template's unused JWT/chat feature.
# They were never populated in this deployment (no migration ever ran) and no
# code references them; drop them if a branch environment created them.
LEGACY_TEMPLATE_TABLES = ("session", "thread", "user", "onboarding_state")


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    """Create every domain table, seed reference data, drop unused template tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    for legacy in LEGACY_TEMPLATE_TABLES:
        if legacy in existing:
            op.drop_table(legacy)

    op.create_table(
        "users",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mattermost_user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("last_synced_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_mattermost_user_id", "users", ["mattermost_user_id"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=False)
    op.create_index("ix_users_email", "users", ["email"], unique=False)

    op.create_table(
        "roles",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
    )
    op.create_index("ix_roles_key", "roles", ["key"], unique=True)

    op.create_table(
        "ceremony_types",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("default_duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.PrimaryKeyConstraint("id", name="pk_ceremony_types"),
    )
    op.create_index("ix_ceremony_types_key", "ceremony_types", ["key"], unique=True)

    op.create_table(
        "cohorts",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("mattermost_team_id", sa.String(length=64), nullable=True),
        sa.Column("mattermost_channel_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("deactivated_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cohorts"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], name="fk_cohorts_created_by_id_users"),
    )
    op.create_index("ux_cohorts_name_lower", "cohorts", [sa.text("lower(name)")], unique=True)
    op.create_index("ix_cohorts_mattermost_team_id", "cohorts", ["mattermost_team_id"], unique=False)
    op.create_index("ix_cohorts_is_active", "cohorts", ["is_active"], unique=False)

    op.create_table(
        "cohort_memberships",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cohort_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("joined_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("assigned_by_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cohort_memberships"),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"], name="fk_cohort_memberships_cohort_id_cohorts"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_cohort_memberships_user_id_users"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_cohort_memberships_role_id_roles"),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"], name="fk_cohort_memberships_assigned_by_id_users"),
        sa.UniqueConstraint("user_id", "cohort_id", name="uq_cohort_memberships_user_cohort"),
    )
    op.create_index("ix_cohort_memberships_cohort_id", "cohort_memberships", ["cohort_id"], unique=False)
    op.create_index("ix_cohort_memberships_user_id", "cohort_memberships", ["user_id"], unique=False)

    op.create_table(
        "sprints",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cohort_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="planned"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("opened_by_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sprints"),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"], name="fk_sprints_cohort_id_cohorts"),
        sa.ForeignKeyConstraint(["opened_by_id"], ["users.id"], name="fk_sprints_opened_by_id_users"),
        sa.UniqueConstraint("cohort_id", "name", name="uq_sprints_cohort_name"),
        sa.CheckConstraint("end_date >= start_date", name="ck_sprints_dates_ordered"),
    )
    op.create_index("ix_sprints_cohort_id", "sprints", ["cohort_id"], unique=False)
    op.create_index("ix_sprints_status", "sprints", ["status"], unique=False)

    op.create_table(
        "ceremonies",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cohort_id", sa.Integer(), nullable=False),
        sa.Column("sprint_id", sa.Integer(), nullable=True),
        sa.Column("ceremony_type_id", sa.Integer(), nullable=False),
        sa.Column("organizer_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", TZ, nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("agenda", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="scheduled"),
        sa.Column("time_expression", sa.String(length=512), nullable=True),
        sa.Column("time_zone", sa.String(length=64), nullable=True),
        sa.Column("channel_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ceremonies"),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"], name="fk_ceremonies_cohort_id_cohorts"),
        sa.ForeignKeyConstraint(["sprint_id"], ["sprints.id"], name="fk_ceremonies_sprint_id_sprints"),
        sa.ForeignKeyConstraint(
            ["ceremony_type_id"], ["ceremony_types.id"], name="fk_ceremonies_ceremony_type_id_ceremony_types"
        ),
        sa.ForeignKeyConstraint(["organizer_id"], ["users.id"], name="fk_ceremonies_organizer_id_users"),
        sa.CheckConstraint("duration_minutes > 0", name="ck_ceremonies_duration_positive"),
    )
    op.create_index("ix_ceremonies_cohort_id", "ceremonies", ["cohort_id"], unique=False)
    op.create_index("ix_ceremonies_sprint_id", "ceremonies", ["sprint_id"], unique=False)
    op.create_index("ix_ceremonies_organizer_id", "ceremonies", ["organizer_id"], unique=False)
    op.create_index("ix_ceremonies_scheduled_at", "ceremonies", ["scheduled_at"], unique=False)
    op.create_index("ix_ceremonies_status", "ceremonies", ["status"], unique=False)

    op.create_table(
        "ceremony_amendments",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ceremony_id", sa.Integer(), nullable=False),
        sa.Column("amended_by_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ceremony_amendments"),
        sa.ForeignKeyConstraint(
            ["ceremony_id"], ["ceremonies.id"], name="fk_ceremony_amendments_ceremony_id_ceremonies"
        ),
        sa.ForeignKeyConstraint(["amended_by_id"], ["users.id"], name="fk_ceremony_amendments_amended_by_id_users"),
    )
    op.create_index("ix_ceremony_amendments_ceremony_id", "ceremony_amendments", ["ceremony_id"], unique=False)

    op.create_table(
        "daily_standups",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sprint_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("what_i_did", sa.Text(), nullable=False),
        sa.Column("what_i_will_do", sa.Text(), nullable=False),
        sa.Column("blockers", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_daily_standups"),
        sa.ForeignKeyConstraint(["sprint_id"], ["sprints.id"], name="fk_daily_standups_sprint_id_sprints"),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"], name="fk_daily_standups_learner_id_users"),
        sa.UniqueConstraint("sprint_id", "learner_id", "log_date", name="uq_daily_standups_sprint_learner_day"),
    )
    op.create_index("ix_daily_standups_sprint_id", "daily_standups", ["sprint_id"], unique=False)
    op.create_index("ix_daily_standups_learner_id", "daily_standups", ["learner_id"], unique=False)
    op.create_index("ix_daily_standups_log_date", "daily_standups", ["log_date"], unique=False)

    op.create_table(
        "escalation_tickets",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_ref", sa.String(length=32), nullable=False),
        sa.Column("cohort_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("assigned_human_id", sa.Integer(), nullable=True),
        sa.Column("ticket_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("status_changed_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("learner_channel_id", sa.String(length=64), nullable=False),
        sa.Column("learner_thread_id", sa.String(length=64), nullable=False),
        sa.Column("human_dm_channel_id", sa.String(length=64), nullable=True),
        sa.Column("human_dm_thread_id", sa.String(length=64), nullable=True),
        sa.Column("sprint_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_escalation_tickets"),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"], name="fk_escalation_tickets_cohort_id_cohorts"),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"], name="fk_escalation_tickets_learner_id_users"),
        sa.ForeignKeyConstraint(
            ["assigned_human_id"], ["users.id"], name="fk_escalation_tickets_assigned_human_id_users"
        ),
        sa.ForeignKeyConstraint(["sprint_id"], ["sprints.id"], name="fk_escalation_tickets_sprint_id_sprints"),
    )
    op.create_index("ix_escalation_tickets_ticket_ref", "escalation_tickets", ["ticket_ref"], unique=True)
    op.create_index("ix_escalation_tickets_cohort_id", "escalation_tickets", ["cohort_id"], unique=False)
    op.create_index("ix_escalation_tickets_learner_id", "escalation_tickets", ["learner_id"], unique=False)
    op.create_index(
        "ix_escalation_tickets_assigned_human_id", "escalation_tickets", ["assigned_human_id"], unique=False
    )
    op.create_index("ix_escalation_tickets_status", "escalation_tickets", ["status"], unique=False)

    op.create_table(
        "onboarding_steps",
        *_audit_columns(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("cohort_id", sa.Integer(), nullable=True),
        sa.Column("step_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("due_at", TZ, nullable=False),
        sa.Column("sent_at", TZ, nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TZ, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_at", TZ, nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("role_key_at_delivery", sa.String(length=64), nullable=True),
        sa.Column("mattermost_post_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding_steps"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_onboarding_steps_user_id_users"),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"], name="fk_onboarding_steps_cohort_id_cohorts"),
        sa.UniqueConstraint(
            "user_id",
            "cohort_id",
            "step_kind",
            name="uq_onboarding_steps_user_cohort_kind",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_onboarding_steps_user_id", "onboarding_steps", ["user_id"], unique=False)
    op.create_index("ix_onboarding_steps_cohort_id", "onboarding_steps", ["cohort_id"], unique=False)
    op.create_index("ix_onboarding_steps_status", "onboarding_steps", ["status"], unique=False)
    op.create_index("ix_onboarding_steps_due_at", "onboarding_steps", ["due_at"], unique=False)

    _seed_reference_data(bind)


def _seed_reference_data(bind: sa.engine.Connection) -> None:
    """Insert the seed rows, skipping any that already exist."""
    roles = sa.table(
        "roles",
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("description", sa.Text),
    )
    for key, label, description in ROLE_SEED:
        bind.execute(
            postgresql.insert(roles)
            .values(key=key, label=label, description=description)
            .on_conflict_do_nothing(index_elements=["key"])
        )

    ceremony_types = sa.table(
        "ceremony_types",
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("default_duration_minutes", sa.Integer),
    )
    for key, label, minutes in CEREMONY_TYPE_SEED:
        bind.execute(
            postgresql.insert(ceremony_types)
            .values(key=key, label=label, default_duration_minutes=minutes)
            .on_conflict_do_nothing(index_elements=["key"])
        )


def downgrade() -> None:
    """Drop only the domain tables, in dependency order. Checkpointer tables are untouched."""
    for table in (
        "onboarding_steps",
        "escalation_tickets",
        "daily_standups",
        "ceremony_reminders",
        "ceremony_amendments",
        "ceremonies",
        "sprints",
        "channel_roles",
        "ceremony_types",
        "roles",
        "users",
    ):
        op.drop_table(table)
