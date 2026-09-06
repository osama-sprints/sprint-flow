"""Seeded ceremony-type lookup."""

from sqlmodel import Field

from app.models.domain_base import DomainBase


class CeremonyType(DomainBase, table=True):
    """A kind of ceremony: standup, planning, review, retrospective, open Q&A.

    Attributes:
        id: Primary key referenced by ``ceremonies.ceremony_type_id``.
        key: Stable machine key, see ``CeremonyTypeKey``.
        label: Display label.
        default_duration_minutes: Used when a ceremony is scheduled without a duration.
    """

    __tablename__ = "ceremony_types"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True, max_length=64)
    label: str = Field(max_length=128)
    default_duration_minutes: int = Field(default=60, nullable=False)
