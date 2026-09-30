"""add_external_event_id_to_ceremonies

Revision ID: d00000000001
Revises: c00000000002
Create Date: 2026-09-15 02:00:00.000000

Adds a nullable ``external_event_id`` column to ``ceremonies`` — the Google
Calendar event id, captured at creation so reschedule/cancel can target the
same external event instead of guessing or re-deriving one. Nullable so
existing rows (and every ceremony scheduled without ``with_meet``, or via the
jitsi provider) are unaffected.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

revision: str = "d00000000001"
down_revision: Union[str, Sequence[str], None] = "e79a5dd80fe3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ceremonies",
        sa.Column("external_event_id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ceremonies", "external_event_id")