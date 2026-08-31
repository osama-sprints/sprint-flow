"""Seed idempotent reference data for SprintFlow."""

from sqlmodel import Session, select

from app.models.ceremony_type import CeremonyType
from app.models.role import Role
from app.services.database import database_service


ROLES = [
    "member",
    "mentor",
    "admin",
]

CEREMONY_TYPES = [
    "standup",
    "planning",
    "review",
    "retrospective",
]


def seed_roles(session: Session) -> None:
    """Insert missing role reference data."""
    for name in ROLES:
        existing = session.exec(
            select(Role).where(Role.name == name)
        ).first()

        if existing is None:
            session.add(Role(name=name))


def seed_ceremony_types(session: Session) -> None:
    """Insert missing ceremony type reference data."""
    for name in CEREMONY_TYPES:
        existing = session.exec(
            select(CeremonyType).where(CeremonyType.name == name)
        ).first()

        if existing is None:
            session.add(CeremonyType(name=name))


def seed_reference_data() -> None:
    """Seed all reference data without creating duplicates."""
    with Session(database_service.engine) as session:
        seed_roles(session)
        seed_ceremony_types(session)
        session.commit()


if __name__ == "__main__":
    seed_reference_data()
    print("Reference data seeded successfully.")
