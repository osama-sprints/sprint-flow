# ai-core/alembic/versions/0005_daily_standup_prompts.py
"""Add proactive daily standup prompts, raw replies, and reply provenance on entries.

Revision ID: 0005_standup_prompts
Revises: e79a5dd80fe3
Create Date: 2026-09-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0005_standup_prompts"
down_revision: Union[str, Sequence[str], None] = "e79a5dd80fe3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "daily_standup_prompts",
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sprint_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", AutoString(length=64), nullable=False),
        sa.Column("dm_channel_id", AutoString(length=64), nullable=True),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone", AutoString(length=64), nullable=False),
        sa.Column("dispatch_at", TZ, nullable=False),
        sa.Column("status", AutoString(length=32), nullable=False),
        sa.Column("prompt_post_id", AutoString(length=128), nullable=True),
        sa.Column("dispatch_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", TZ, nullable=True),
        sa.Column("claimed_at", TZ, nullable=True),
        sa.Column("claimed_by", AutoString(length=128), nullable=True),
        sa.Column("dispatched_at", TZ, nullable=True),
        sa.Column("answered_at", TZ, nullable=True),
        sa.Column("closed_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_daily_standup_prompts"),
        sa.ForeignKeyConstraint(["sprint_id"], ["sprints.id"], name="fk_daily_standup_prompts_sprint_id"),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"], name="fk_daily_standup_prompts_learner_id"),
        sa.UniqueConstraint(
            "sprint_id", "learner_id", "local_date", name="uq_daily_standup_prompts_sprint_learner_day"
        ),
    )
    op.create_index(op.f("ix_daily_standup_prompts_sprint_id"), "daily_standup_prompts", ["sprint_id"], unique=False)
    op.create_index(op.f("ix_daily_standup_prompts_learner_id"), "daily_standup_prompts", ["learner_id"], unique=False)
    op.create_index(op.f("ix_daily_standup_prompts_channel_id"), "daily_standup_prompts", ["channel_id"], unique=False)
    op.create_index(
        op.f("ix_daily_standup_prompts_dm_channel_id"), "daily_standup_prompts", ["dm_channel_id"], unique=False
    )
    op.create_index(op.f("ix_daily_standup_prompts_local_date"), "daily_standup_prompts", ["local_date"], unique=False)
    op.create_index(op.f("ix_daily_standup_prompts_status"), "daily_standup_prompts", ["status"], unique=False)

    op.create_table(
        "standup_replies",
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("dm_channel_id", AutoString(length=64), nullable=False),
        sa.Column("post_id", AutoString(length=128), nullable=False),
        sa.Column("post_root_id", AutoString(length=128), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=True),
        sa.Column("outcome", AutoString(length=32), nullable=False),
        sa.Column("received_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_standup_replies"),
        sa.ForeignKeyConstraint(["prompt_id"], ["daily_standup_prompts.id"], name="fk_standup_replies_prompt_id"),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"], name="fk_standup_replies_learner_id"),
        sa.UniqueConstraint("post_id", name="uq_standup_replies_post_id"),
    )
    op.create_index(op.f("ix_standup_replies_prompt_id"), "standup_replies", ["prompt_id"], unique=False)
    op.create_index(op.f("ix_standup_replies_learner_id"), "standup_replies", ["learner_id"], unique=False)
    op.create_index(op.f("ix_standup_replies_dm_channel_id"), "standup_replies", ["dm_channel_id"], unique=False)
    op.create_index(op.f("ix_standup_replies_outcome"), "standup_replies", ["outcome"], unique=False)

    # Reply provenance on manually-created entries: NULL for everything written
    # by the older (chat tool) path, set for rows collected through a prompt.
    op.add_column("daily_standups", sa.Column("prompt_id", sa.Integer(), nullable=True))
    op.add_column("daily_standups", sa.Column("raw_response", sa.Text(), nullable=True))
    op.add_column("daily_standups", sa.Column("submitted_at", TZ, nullable=True))
    op.add_column("daily_standups", sa.Column("timezone", AutoString(length=64), nullable=True))
    op.create_foreign_key(
        "fk_daily_standups_prompt_id", "daily_standups", "daily_standup_prompts", ["prompt_id"], ["id"]
    )
    op.create_unique_constraint("uq_daily_standups_prompt", "daily_standups", ["prompt_id"])
    op.create_index(op.f("ix_daily_standups_prompt_id"), "daily_standups", ["prompt_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_daily_standups_prompt_id"), table_name="daily_standups")
    op.drop_constraint("uq_daily_standups_prompt", "daily_standups", type_="unique")
    op.drop_constraint("fk_daily_standups_prompt_id", "daily_standups", type_="foreignkey")
    op.drop_column("daily_standups", "timezone")
    op.drop_column("daily_standups", "submitted_at")
    op.drop_column("daily_standups", "raw_response")
    op.drop_column("daily_standups", "prompt_id")

    op.drop_index(op.f("ix_standup_replies_outcome"), table_name="standup_replies")
    op.drop_index(op.f("ix_standup_replies_dm_channel_id"), table_name="standup_replies")
    op.drop_index(op.f("ix_standup_replies_learner_id"), table_name="standup_replies")
    op.drop_index(op.f("ix_standup_replies_prompt_id"), table_name="standup_replies")
    op.drop_table("standup_replies")

    op.drop_index(op.f("ix_daily_standup_prompts_status"), table_name="daily_standup_prompts")
    op.drop_index(op.f("ix_daily_standup_prompts_local_date"), table_name="daily_standup_prompts")
    op.drop_index(op.f("ix_daily_standup_prompts_dm_channel_id"), table_name="daily_standup_prompts")
    op.drop_index(op.f("ix_daily_standup_prompts_channel_id"), table_name="daily_standup_prompts")
    op.drop_index(op.f("ix_daily_standup_prompts_learner_id"), table_name="daily_standup_prompts")
    op.drop_index(op.f("ix_daily_standup_prompts_sprint_id"), table_name="daily_standup_prompts")
    op.drop_table("daily_standup_prompts")
