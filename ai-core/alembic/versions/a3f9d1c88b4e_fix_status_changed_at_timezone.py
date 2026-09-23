"""fix announcements.status_changed_at to be timezone-aware.

Revision ID: a3f9d1c88b4e
Revises: 88dc2e879c6b
Create Date: 2026-09-21

The original create-announcements-table migration used
TIMESTAMP WITHOUT TIME ZONE for this one column, matching a bug in the
model at the time (plain datetime.utcnow() instead of the project's
TZ_DATETIME + utcnow() convention used by every other timestamped table).
That migration already ran against real databases, so this is a separate
ALTER rather than an edit to the original — editing an already-applied
migration's upgrade() does not retroactively change existing columns.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a3f9d1c88b4e'
down_revision: Union[str, Sequence[str], None] = 'd32cf4519a86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing naive values are assumed to already be UTC (the model always
    # wrote UTC via datetime.utcnow()), so USING ... AT TIME ZONE 'UTC'
    # reinterprets them correctly instead of shifting them.
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
