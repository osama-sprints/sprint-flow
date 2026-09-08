"""add_ceremony_reminder_table

Revision ID: c00000000001
Revises: 9199422f052d
Create Date: 2026-09-08 13:22:00.000000

Adds the ``ceremony_reminders`` table that the reminder poller uses as its
idempotency guard. A unique constraint on (ceremony_id, recipient_mm_id, window)
prevents double-sending on restart: an INSERT that would violate the constraint
raises IntegrityError; the poller catches it and skips without re-posting the DM.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c00000000001"
down_revision: Union[str, Sequence[str], None] = "9199422f052d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ceremony_reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ceremony_id", sa.Integer(), nullable=False),
        sa.Column("recipient_mm_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("window", sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ceremony_id"],
            ["ceremonies.id"],
            name=op.f("ceremony_reminders_ceremony_id_fkey"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ceremony_reminders")),
        sa.UniqueConstraint(
            "ceremony_id",
            "recipient_mm_id",
            "window",
            name="uq_ceremony_reminder_ceremony_recipient_window",
        ),
    )
    op.create_index(
        op.f("ix_ceremony_reminders_ceremony_id"),
        "ceremony_reminders",
        ["ceremony_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ceremony_reminders_recipient_mm_id"),
        "ceremony_reminders",
        ["recipient_mm_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ceremony_reminders_recipient_mm_id"),
        table_name="ceremony_reminders",
    )
    op.drop_index(
        op.f("ix_ceremony_reminders_ceremony_id"),
        table_name="ceremony_reminders",
    )
    op.drop_table("ceremony_reminders")
