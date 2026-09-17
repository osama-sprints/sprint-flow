"""add team and channel to ceremony

Revision ID: b00000000000
Revises: a1e6c9f0d3b2
Create Date: 2026-09-07 16:46:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b00000000000"
down_revision: Union[str, Sequence[str], None] = "0002_sprints_name_ci"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add team_id
    op.add_column(
        "ceremonies",
        sa.Column("team_id", sqlmodel.sql.sqltypes.AutoString(), server_default="sprints-community", nullable=False),
    )
    op.create_index(op.f("ix_ceremonies_team_id"), "ceremonies", ["team_id"], unique=False)

    # Make channel_id non-nullable. First we ensure it has a value if there are any nulls
    op.execute("UPDATE ceremonies SET channel_id = 'default-channel' WHERE channel_id IS NULL")
    op.alter_column("ceremonies", "channel_id", existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=False)
    # channel_id might not have an index yet depending on previous migrations, let's create it
    op.create_index(op.f("ix_ceremonies_channel_id"), "ceremonies", ["channel_id"], unique=False)

    # Make cohort_id nullable
    op.alter_column("ceremonies", "cohort_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute(
        "DELETE FROM ceremony_amendments WHERE ceremony_id IN (SELECT id FROM ceremonies WHERE cohort_id IS NULL)"
    )
    op.execute("DELETE FROM ceremonies WHERE cohort_id IS NULL")
    op.alter_column("ceremonies", "cohort_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index(op.f("ix_ceremonies_channel_id"), table_name="ceremonies")
    op.alter_column("ceremonies", "channel_id", existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=True)
    op.drop_index(op.f("ix_ceremonies_team_id"), table_name="ceremonies")
    op.drop_column("ceremonies", "team_id")
