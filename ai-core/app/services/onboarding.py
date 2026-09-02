
import asyncio
from datetime import ( UTC, datetime, timedelta, )
from typing import Optional

from sqlmodel import ( Session, col, select,)

from app.core.logging import logger
from app.models.onboarding import OnboardingState
from app.services.database import database_service
from app.services.mattermost import mattermost_client


_POLL_INTERVAL_SECONDS = 300


_FOLLOWUP_DELAY = timedelta(days=3)


_MAX_FOLLOWUPS = 1

_DEFAULT_ROLE = "member"


async def resolve_role(mattermost_user_id: str) -> Optional[str]:

    try:
        
        from app.models.cohort_membership import CohortMembership
        from app.models.role import Role
        from app.services.identity import get_user_by_mattermost_id
    except ImportError:
        logger.warning("onboarding_role_models_unavailable", user_id=mattermost_user_id)
        return None

    user = get_user_by_mattermost_id(mattermost_user_id)
    if not user:
        return None 

    with Session(database_service.engine) as session:
        stmt = (
            select(Role.name)
            .join(CohortMembership, CohortMembership.role_id == Role.id)
            .where(CohortMembership.user_id == user.id , CohortMemebership.status == "active", )
            .order_by(col(CohortMembership.joined_at).desc())
        )
        return session.exec(stmt).first()


def _greeting_for(role: Optional[str]) -> str:
   
    if role:
        return (
            f"Welcome to SprintFlow! You've joined as a **{role}**. "
            "I'll check in with you over the next few days to help you get oriented."
        )
    return (
        "Welcome to SprintFlow! Glad to have you here -- "
        "I'll follow up shortly once your role is set up."
    )


async def handle_arrival(mattermost_user_id: str) -> None:

    with Session(database_service.engine) as session:
        state = session.get(OnboardingState, mattermost_user_id)
        if state and state.greeted_at:
            logger.info("onboarding_already_greeted", user_id=mattermost_user_id)
            return

        role = await resolve_role(mattermost_user_id)

        state = state or OnboardingState(user_id=mattermost_user_id)
        state.role = role or _DEFAULT_ROLE
        state.greeted_at = datetime.now(UTC)
        
        state.next_followup_due_at = datetime.now(UTC) + _FOLLOWUP_DELAY
        session.add(state)
        session.commit()

    channel = await mattermost_client.create_direct_channel(mattermost_user_id)
    if not channel:
        logger.warning("onboarding_greet_dm_failed", user_id=mattermost_user_id)
        return

    await mattermost_client.create_post(channel["id"], _greeting_for(role))
    logger.info("onboarding_greeted", user_id=mattermost_user_id, role=role)


async def _send_followup(state: OnboardingState) -> None:
   
    
    role = await resolve_role(state.user_id) or state.role

    channel = await mattermost_client.create_direct_channel(state.user_id)
    if not channel:
        logger.warning("onboarding_followup_dm_failed", user_id=state.user_id)
        return

    text = (
        f"Checking in -- how's it going so far as a **{role}**? Let me know if you need anything."
        if role
        else "Checking in -- how's it going so far? Let me know if you need anything."
    )
    await mattermost_client.create_post(channel["id"], text)

    state.role = role or state.role
    state.followups_sent += 1
    state.next_followup_due_at = None
    logger.info("onboarding_followup_sent", user_id=state.user_id, role=role)


async def followup_poller() -> None:

    while True:
        try:
            with Session(database_service.engine) as session:
                due = session.exec(
                    select(OnboardingState).where(
                        col(OnboardingState.next_followup_due_at).is_not(None),
                        col(OnboardingState.next_followup_due_at) <= datetime.now(UTC),
                        col(OnboardingState.followups_sent) < _MAX_FOLLOWUPS,
                    )
                ).all()
                for state in due:
                    await _send_followup(state)
                    session.add(state)
                session.commit()
        except Exception:
            logger.exception("onboarding_followup_poll_failed")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)