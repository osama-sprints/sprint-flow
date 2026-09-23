from app.core.database import engine
from sqlmodel import SQLModel

SQLModel.metadata.drop_all(engine)
print("Dropped all tables via SQLModel.metadata")
