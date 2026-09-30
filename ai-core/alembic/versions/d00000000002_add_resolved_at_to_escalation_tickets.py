"""add_resolved_at_to_escalation_tickets

Revision ID: d00000000002
Revises: d00000000001
Create Date: 2026-09-16 09:00:00.000000

Adds the nullable ``resolved_at`` timestamp that ``set_escalation_status``
already writes to on resolution but which was never added to the model or
schema, causing every resolution attempt to crash with
``"EscalationTicket" object has no field "resolved_at"``.
"""

from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d00000000002"
down_revision: Union[str, Sequence[str], None] = "d00000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "escalation_tickets",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("escalation_tickets", "resolved_at")
