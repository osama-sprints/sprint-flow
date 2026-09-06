"""Idempotent seeding of the lookup tables.

Reference data is data, not code: the migration that creates ``roles`` and
``ceremony_types`` seeds them, and this function re-applies the same keys at
every startup and from ``make seed`` so a second run changes nothing and a
missing row is always restored. Labels and descriptions are refreshed; keys
are never removed here.
"""

from typing import NamedTuple

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.logging import logger
from app.models import (
    CeremonyType,
    Role,
    utcnow,
)
from app.models.enums import (
    CEREMONY_TYPE_DEFAULT_DURATION_MINUTES,
    CEREMONY_TYPE_LABELS,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    CeremonyTypeKey,
    RoleKey,
)
from app.services.database import session_scope


class SeedSummary(NamedTuple):
    """What a seed run touched."""

    roles: int
    ceremony_types: int


async def seed_reference_data(session: AsyncSession | None = None) -> SeedSummary:
    """Insert missing roles and ceremony types; refresh labels on existing ones.

    Args:
        session: Optional session to reuse.

    Returns:
        SeedSummary: Counts of rows written (inserted or refreshed).
    """
    now = utcnow()
    async with session_scope(session) as s:
        for role_key in RoleKey:
            statement = (
                pg_insert(Role)
                .values(
                    key=role_key.value,
                    label=ROLE_LABELS[role_key],
                    description=ROLE_DESCRIPTIONS[role_key],
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={
                        "label": ROLE_LABELS[role_key],
                        "description": ROLE_DESCRIPTIONS[role_key],
                        "updated_at": now,
                    },
                )
            )
            await s.exec(statement)

        for type_key in CeremonyTypeKey:
            statement = (
                pg_insert(CeremonyType)
                .values(
                    key=type_key.value,
                    label=CEREMONY_TYPE_LABELS[type_key],
                    default_duration_minutes=CEREMONY_TYPE_DEFAULT_DURATION_MINUTES[type_key],
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={
                        "label": CEREMONY_TYPE_LABELS[type_key],
                        "default_duration_minutes": CEREMONY_TYPE_DEFAULT_DURATION_MINUTES[type_key],
                        "updated_at": now,
                    },
                )
            )
            await s.exec(statement)

    summary = SeedSummary(roles=len(RoleKey), ceremony_types=len(CeremonyTypeKey))
    logger.info("reference_data_seeded", roles=summary.roles, ceremony_types=summary.ceremony_types)
    return summary
