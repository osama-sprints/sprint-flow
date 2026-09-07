"""Seeded role lookup. New roles are rows, not migrations."""

from sqlalchemy import Text
from sqlmodel import Field

from app.models.domain_base import DomainBase


class Role(DomainBase, table=True):
    """A cohort-scoped role definition.

    Attributes:
        id: Primary key referenced by ``cohort_memberships.role_id``.
        key: Stable machine key (``learner``, ``tech_lead``, ...), see ``RoleKey``.
        label: Display label shown to people.
        description: What the role does, for onboarding and prompts.
    """

    __tablename__ = "roles"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True, max_length=64)
    label: str = Field(max_length=128)
    description: str | None = Field(default=None, sa_type=Text)
