from app.core.database import engine
from sqlmodel import SQLModel
from app.models.ceremony import Ceremony, CeremonyType
from app.models.learning import DomainEntity, OnboardingState

SQLModel.metadata.drop_all(engine)
print("Dropped all tables via SQLModel.metadata")
