"""Durable outbox of onboarding deliveries.

Each row is one message that must be delivered exactly once: the welcome, a
channel orientation, or a later follow-up. Rows are inserted with
``ON CONFLICT DO NOTHING`` on the unique key, so a replayed arrival event or a
second dispatcher can never create a duplicate, and they are claimed with a
lease before delivery so two workers can never send the same one.
"""

from datetime import datetime

from sqlalchemy import (
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)
from app.models.enums import OnboardingStepStatus


class OnboardingStep(DomainBase, table=True):
    """One scheduled onboarding delivery for one person.

    Attributes:
        id: Primary key.
        user_id: The person being onboarded.
        team_id: The Mattermost team.
        channel_id: The Mattermost channel the step is about; NULL for workspace-level steps
            (the welcome and the follow-up).
        step_kind: ``welcome``, ``orientation`` or ``follow_up``.
        status: ``pending``, ``sent``, ``failed`` or ``halted``.
        due_at: Earliest instant the step may be delivered.
        sent_at: When delivery succeeded; NULL until then.
        attempt_count: Delivery attempts so far.
        next_attempt_at: Earliest retry after a failure.
        last_error: The most recent delivery error, for operators.
        claimed_at: Lease start; a worker that crashes releases it by timeout.
        claimed_by: Identifier of the worker holding the lease.
        role_key_at_delivery: The role the content was tailored to.
        mattermost_post_id: The DM post that was created, for verification.
    """

    __tablename__ = "onboarding_steps"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "channel_id",
            "step_kind",
            name="uq_onboarding_steps_user_channel_kind",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    team_id: str | None = Field(default="sprints-community", max_length=64, index=True)
    channel_id: str | None = Field(default=None, max_length=64, index=True)
    step_kind: str = Field(max_length=32)
    status: str = Field(default=OnboardingStepStatus.PENDING.value, nullable=False, max_length=32, index=True)
    due_at: datetime = Field(sa_type=TZ_DATETIME, nullable=False, index=True)
    sent_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    attempt_count: int = Field(default=0, nullable=False)
    next_attempt_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    last_error: str | None = Field(default=None, sa_type=Text)
    claimed_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
    claimed_by: str | None = Field(default=None, max_length=128)
    role_key_at_delivery: str | None = Field(default=None, max_length=64)
    mattermost_post_id: str | None = Field(default=None, max_length=64)
