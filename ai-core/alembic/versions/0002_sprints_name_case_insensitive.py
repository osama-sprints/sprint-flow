"""Case-insensitive uniqueness for sprint names within a cohort.

Revision ID: 0002_sprints_name_ci
Revises: 0001_sprintflow_domain
Create Date: 2026-09-03

The back office resolves sprint names case-insensitively ("Sprint 1" and
"sprint 1" are the same sprint), but ``uq_sprints_cohort_name`` only rejects
exact repeats. Under a concurrent race two differently-cased inserts could
both succeed. This functional unique index makes the database enforce the
same rule the service applies, so the service's ``IntegrityError`` re-read
path covers the race. ``if_not_exists`` keeps the revision safe on a database
that received the index by other means.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_sprints_name_ci"
down_revision: Union[str, Sequence[str], None] = "0001_sprintflow_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the functional unique index on (cohort_id, lower(name)).

    Raises:
        RuntimeError: When existing rows already violate the rule, naming them.
            The index cannot be created over duplicates, and PostgreSQL's own
            error does not say which rows are at fault — this one does, so an
            operator can rename or delete them and run the migration again.
    """
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT cohort_id, lower(name) AS key, array_agg(id ORDER BY id) AS ids "
                "FROM sprints GROUP BY cohort_id, lower(name) HAVING count(*) > 1"
            )
        )
        .all()
    )
    if duplicates:
        listed = "; ".join(f"cohort {row.cohort_id} name {row.key!r} sprint ids {list(row.ids)}" for row in duplicates)
        raise RuntimeError(
            "Cannot create ux_sprints_cohort_name_lower: sprint names that differ only by case "
            f"already exist. Rename or remove one of each pair, then migrate again. Duplicates: {listed}"
        )

    op.create_index(
        "ux_sprints_cohort_name_lower",
        "sprints",
        ["cohort_id", sa.text("lower(name)")],
        unique=True,
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the functional unique index."""
    op.drop_index("ux_sprints_cohort_name_lower", table_name="sprints", if_exists=True)
