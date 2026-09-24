"""create announcements table.
 
Revision ID: 88dc2e879c6b
Revises: 41cec28ef92a
Create Date: 2026-09-15 09:54:53.077676
 
"""
from typing import Sequence, Union
 
import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op
 
 
# revision identifiers, used by Alembic.
revision = '88dc2e879c6b'
down_revision = ('9199422f052d', 'e79a5dd80fe3', '0004_escalation_raw_response')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
 
 
def upgrade() -> None:
    """Upgrade schema."""
    # status_changed_at is deliberately TIMESTAMP WITHOUT TIME ZONE here: that is
    # what this migration originally created, and a3f9d1c88b4e converts it.
    op.create_table(
        'announcements',
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('requester_id', sa.Integer(), nullable=False),
        sa.Column('cohort_id', sa.Integer(), nullable=False),
        sa.Column('resolved_channel_id', sa.String(), nullable=False),
        sa.Column('exact_text', sa.String(), nullable=False),
        sa.Column('delivery_mode', sa.String(), nullable=False),
        sa.Column('confirmation_status', sa.String(), nullable=False),
        sa.Column('mattermost_post_id', sa.String(), nullable=True),
        sa.Column(
            'outcome',
            sa.Enum('PENDING', 'SENT', 'CANCELLED', 'RATE_LIMITED', 'UNAUTHORIZED', 'FAILED', name='announcementoutcome'),
            nullable=False,
        ),
        sa.Column('status_changed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['requester_id'], ['users.id']),
        sa.ForeignKeyConstraint(['cohort_id'], ['sprints.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_announcements_resolved_channel_id'), 'announcements', ['resolved_channel_id'])
    op.create_index(op.f('ix_announcements_cohort_id'), 'announcements', ['cohort_id'])
    op.create_index(op.f('ix_announcements_requester_id'), 'announcements', ['requester_id'])
 
 
def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_announcements_requester_id'), table_name='announcements')
    op.drop_index(op.f('ix_announcements_cohort_id'), table_name='announcements')
    op.drop_index(op.f('ix_announcements_resolved_channel_id'), table_name='announcements')
    op.drop_table('announcements')
    sa.Enum(name='announcementoutcome').drop(op.get_bind(), checkfirst=True)
