"""add_meet_link_to_ceremonies

Revision ID: c00000000002
Revises: c00000000001
Create Date: 2026-09-08 13:37:00.000000

Adds a nullable ``meet_link`` column to ``ceremonies`` to store the Google Meet
join URL that is optionally created at scheduling time.  The column is nullable
so existing rows are unaffected; the calendar API failure path also leaves it
NULL so scheduling is never blocked by a Meet outage.
"""

from typing import (
    Sequence,
    Union,
)

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c00000000002"
down_revision: Union[str, Sequence[str], None] = "c00000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ceremonies",
        sa.Column(
            "meet_link",
            sqlmodel.sql.sqltypes.AutoString(length=512),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("ceremonies", "meet_link")
