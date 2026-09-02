import asyncio
from app.services.database import database_service
from app.models.user import User
from sqlmodel import Session

def main():
    with Session(database_service.engine) as session:
        user = User(
            mattermost_user_id="223101294",
            username="admin",
            email="admin@sprintflow.ai",
            hashed_password="placeholder"
        )
        session.add(user)
        session.commit()
        print("User inserted!")

if __name__ == "__main__":
    main()
