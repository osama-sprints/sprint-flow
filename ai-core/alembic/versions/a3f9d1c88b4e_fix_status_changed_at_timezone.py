"""fix announcements.status_changed_at to be timezone-aware.

Revision ID: a3f9d1c88b4e
Revises: d32cf4519a86
Create Date: 2026-09-21

The merge revision named ``create_announcements_table`` (88dc2e879c6b) was
shipped with an empty ``upgrade()``, so no revision in this chain ever created
the ``announcements`` table even though the model declares it. Fresh databases
therefore crashed here with ``relation "announcements" does not exist`` and the
ai-core container entered a restart loop.

This revision restores the table (create-if-missing, so databases that somehow
already have it are untouched) and converts ``status_changed_at`` to
``TIMESTAMP WITH TIME ZONE``. Existing naive values are assumed to already be
UTC (the model always wrote UTC via ``datetime.utcnow()``), so
``USING ... AT TIME ZONE 'UTC'`` reinterprets them correctly instead of
shifting them.

Downgrade only reverses the column type. The table itself is never dropped:
databases that had it before this revision must keep it, and fresh databases
without it have nothing to remove.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a3f9d1c88b4e"
down_revision: Union[str, Sequence[str], None] = "d32cf4519a86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _announcements_table_missing() -> bool:
    bind = op.get_bind()
    return not sa.inspect(bind).has_table("announcements")


def upgrade() -> None:
    """Create the missing announcements table, then enforce tz-awareness."""
    if _announcements_table_missing():
        # Mirrors app.models.announcement.Announcement (DomainBase audit
        # columns included). The cohort_id column keeps the model's legacy
        # attribute name; it references sprints.id exactly as the model does.
        op.create_table(
            "announcements",
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("requester_id", sa.Integer(), nullable=False),
            sa.Column("cohort_id", sa.Integer(), nullable=False),
            sa.Column("resolved_channel_id", sa.String(length=64), nullable=False),
            sa.Column("exact_text", sa.Text(), nullable=False),
            sa.Column("delivery_mode", sa.String(), nullable=False),
            sa.Column(
                "confirmation_status",
                sa.String(),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("mattermost_post_id", sa.String(length=64), nullable=True),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["requester_id"], ["users.id"], name="fk_announcements_requester_id_users"),
            sa.ForeignKeyConstraint(["cohort_id"], ["sprints.id"], name="fk_announcements_cohort_id_sprints"),
            sa.PrimaryKeyConstraint("id", name="pk_announcements"),
        )
        op.create_index(op.f("ix_announcements_requester_id"), "announcements", ["requester_id"], unique=False)
        op.create_index(op.f("ix_announcements_cohort_id"), "announcements", ["cohort_id"], unique=False)
        op.create_index(
            op.f("ix_announcements_resolved_channel_id"), "announcements", ["resolved_channel_id"], unique=False
        )

    op.execute(
        "ALTER TABLE announcements "
        "ALTER COLUMN status_changed_at TYPE TIMESTAMP WITH TIME ZONE "
        "USING status_changed_at AT TIME ZONE 'UTC'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE announcements "
        "ALTER COLUMN status_changed_at TYPE TIMESTAMP WITHOUT TIME ZONE "
        "USING status_changed_at AT TIME ZONE 'UTC'"
    )
