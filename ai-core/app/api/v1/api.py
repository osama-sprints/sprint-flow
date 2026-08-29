"""API v1 router configuration.

This module sets up the main API router and includes the Mattermost transport
router. There is no HTTP-facing chat or auth API: SprintFlow talks to the agent
through Mattermost.
"""

from fastapi import APIRouter

from app.api.v1.mattermost import router as mattermost_router
from app.core.logging import logger

api_router = APIRouter()

# Include routers.
#
# The template's /auth and /chatbot routers were removed. SprintFlow
# authenticates entirely through Mattermost and reaches the agent over the
# webhook and websocket transports, so those endpoints were unreachable in
# practice — and unusable anyway, since their tables are Alembic-managed and no
# migration is ever run here. Leaving them registered meant an unauthenticated
# route answering 500 on a published port.
api_router.include_router(mattermost_router, prefix="/mattermost", tags=["Mattermost"])


@api_router.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        dict: Health status information.
    """
    logger.info("health_check_called")
    return {"status": "healthy", "version": "1.0.0"}
