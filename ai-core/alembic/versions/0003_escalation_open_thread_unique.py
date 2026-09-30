"""Prevent duplicate open escalations for the same learner thread.

Revision ID: 0003_escalation_open_thread_unique
Revises: 0002_sprints_name_ci
Create Date: 2026-09-08

A retried webhook delivery, or the model calling ``escalate_to_human`` twice
for one turn, must not open a second ticket or send a second DM for the same
question. ``app.services.escalation.open_escalation`` checks for an existing
non-resolved ticket on the same ``learner_thread_id`` before creating one, but
that check-then-act has the same race the sprint-name check has (see
``0002_sprints_name_ci``): two concurrent calls can both pass the check before
either writes. This partial unique index makes the database enforce the same
rule, so the service's ``IntegrityError`` re-read path (mirroring
``back_office.create_cohort``) covers the race.

Partial (``status <> 'resolved'``), not a plain unique index: a thread can
legitimately be escalated more than once over its lifetime — once resolved, a
follow-up question in the same conversation opens a new ticket. Only one
ticket may be *in flight* (``open`` or ``waiting_human``) for a given thread
at a time. ``if_not_exists`` keeps the revision safe on a database that
received the index by other means.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_escalation_open_thread"
down_revision: Union[str, Sequence[str], None] = "0002_sprints_name_ci"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the partial unique index on (learner_thread_id) where not resolved.

    Raises:
        RuntimeError: When existing rows already violate the rule, naming
            them, the same defensive check ``0002_sprints_name_ci`` performs
            before creating its own unique index — the index cannot be
            created over duplicates, and PostgreSQL's own error does not say
            which rows are at fault.
    """
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT learner_thread_id, array_agg(id ORDER BY id) AS ids FROM escalation_tickets "
                "WHERE status <> 'resolved' GROUP BY learner_thread_id HAVING count(*) > 1"
            )
        )
        .all()
    )
    if duplicates:
        listed = "; ".join(f"thread {row.learner_thread_id!r} ticket ids {list(row.ids)}" for row in duplicates)
        raise RuntimeError(
            "Cannot create ux_escalation_tickets_open_thread: more than one non-resolved ticket "
            f"already exists for the same learner thread. Resolve or merge one of each pair, then "
            f"migrate again. Duplicates: {listed}"
        )

    op.create_index(
        "ux_escalation_tickets_open_thread",
        "escalation_tickets",
        ["learner_thread_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the partial unique index."""
    op.drop_index("ux_escalation_tickets_open_thread", table_name="escalation_tickets", if_exists=True)
