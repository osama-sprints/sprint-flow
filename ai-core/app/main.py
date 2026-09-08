"""This file contains the main application entry point."""

import asyncio
from contextlib import (
    asynccontextmanager,
    suppress,
)
from datetime import (
    UTC,
    datetime,
)

from asgi_correlation_id import CorrelationIdMiddleware
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.api import api_router
from app.core.cache import cache_service
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.metrics import setup_metrics
from app.core.middleware import (
    LoggingContextMiddleware,
    MetricsMiddleware,
    ProfilingMiddleware,
)
from app.core.observability import langfuse_init, shutdown_langfuse
from app.services.agent import agent
from app.services.database import database_service
from app.services.domain.reference_data import seed_reference_data
from app.services.mattermost import mattermost_client
from app.services.mattermost_ws import mattermost_ws_listener
from app.services.memory import memory_service
from app.workers.onboarding_dispatcher import onboarding_dispatcher
from app.services.ceremony_reminders import reminder_poller

# Load environment variables
load_dotenv()
langfuse_init()

# The table whose absence means the domain migrations never ran. /health
# reports 503 in that case so the container cannot look healthy while every
# cohort-aware feature is broken.
_SCHEMA_SENTINEL_TABLE = "cohorts"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown events."""
    logger.info(
        "application_startup",
        project_name=settings.PROJECT_NAME,
        version=settings.VERSION,
        api_prefix=settings.API_V1_STR,
    )

    # Initialize cache service (connects to Valkey if configured)
    try:
        await cache_service.initialize()
    except Exception as e:
        logger.exception("cache_initialization_failed", error=str(e))

    # Pre-warm the LangGraph agent: create graph + connection pool at startup
    # to avoid cold-start latency on the first request
    try:
        await agent.create_graph()
        logger.info("graph_pre_warmed")
    except Exception as e:
        logger.exception("graph_pre_warm_failed", error=str(e))

    # Pre-warm mem0 AsyncMemory: builds the Qdrant client and checks the
    # collection, so the first search() or add() doesn't pay the cold-init cost
    try:
        await memory_service.initialize()
    except Exception as e:
        logger.exception("memory_service_pre_warm_failed", error=str(e))

    # Sprint 1 / data model: reference data is data, not code. Re-applying the
    # seed at every start is idempotent and restores anything removed by hand.
    try:
        await seed_reference_data()
    except Exception as e:
        logger.exception("reference_data_seed_failed", error=str(e))

    # Start the Mattermost WebSocket listener. This is what makes direct
    # messages work at all — outgoing webhooks never fire outside public
    # channels. It reconnects on its own, so a Mattermost that is still booting
    # is not a startup failure.
    try:
        await mattermost_ws_listener.start()
    except Exception as e:
        logger.exception("mattermost_ws_start_failed", error=str(e))

    # Sprint 1 / onboarding: durable follow-ups are delivered by a background
    # dispatcher reading the onboarding outbox, so they survive restarts.
    try:
        await onboarding_dispatcher.start()
    except Exception as e:
        logger.exception("onboarding_dispatcher_start_failed", error=str(e))

    # Proactive ceremony reminders: DMs sent 24 h and 1 h before each ceremony.
    # The CeremonyReminder table guarantees at-most-once delivery on restart.
    try:
        ceremony_reminder_task = asyncio.create_task(
            reminder_poller(), name="ceremony-reminder-poller"
        )
    except Exception as e:
        ceremony_reminder_task = None
        logger.exception("ceremony_reminder_poller_start_failed", error=str(e))

    yield

    # Cleanup on shutdown
    if ceremony_reminder_task is not None and not ceremony_reminder_task.done():
        ceremony_reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await ceremony_reminder_task
    await onboarding_dispatcher.stop()
    await mattermost_ws_listener.stop()
    await cache_service.close()
    await mattermost_client.close()
    await database_service.close()
    if agent._connection_pool:
        await agent._connection_pool.close()
        logger.info("connection_pool_closed")
    # Flush pending Langfuse traces before shutdown
    shutdown_langfuse()
    logger.info("application_shutdown")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=settings.DESCRIPTION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Set up Prometheus metrics
setup_metrics(app)

# Add logging context middleware (must be added before other middleware to capture context)
app.add_middleware(LoggingContextMiddleware)

# Add custom metrics middleware
app.add_middleware(MetricsMiddleware)

# Add profiling middleware (DEBUG only — saves HTML to /tmp on slow requests)
if settings.DEBUG:
    app.add_middleware(ProfilingMiddleware)

# Add correlation ID middleware — must be outermost so request_id is set before all others
app.add_middleware(CorrelationIdMiddleware)

# Set up rate limiter exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # pyright: ignore[reportArgumentType]


# Add validation exception handler
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors from request data.

    Args:
        request: The request that caused the validation error
        exc: The validation error

    Returns:
        JSONResponse: A formatted error response
    """
    # Log the validation error
    logger.error(
        "validation_error",
        client_host=request.client.host if request.client else "unknown",
        path=request.url.path,
        errors=str(exc.errors()),
    )

    # Format the errors to be more user-friendly
    formatted_errors = []
    for error in exc.errors():
        loc = " -> ".join([str(loc_part) for loc_part in error["loc"] if loc_part != "body"])
        formatted_errors.append({"field": loc, "message": error["msg"]})

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": formatted_errors},
    )


# Set up CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["root"][0])
async def root(request: Request):
    """Root endpoint returning basic API information."""
    logger.info("root_endpoint_called")
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "healthy",
        "environment": settings.ENVIRONMENT.value,
        "swagger_url": "/docs",
        "redoc_url": "/redoc",
    }


@app.get("/health")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["health"][0])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint with environment-specific information.

    Returns:
        JSONResponse: Health status payload, with HTTP 503 when the database
        is unreachable or the domain schema was never migrated, so load
        balancers (and Compose) can drop the instance.
    """
    logger.info("health_check_called")

    db_healthy = await database_service.health_check()
    schema_present = db_healthy and await database_service.table_exists(_SCHEMA_SENTINEL_TABLE)
    healthy = db_healthy and schema_present

    response = {
        "status": "healthy" if healthy else "degraded",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT.value,
        "components": {
            "api": "healthy",
            "database": "healthy" if db_healthy else "unhealthy",
            "domain_schema": "healthy" if schema_present else "missing",
            "onboarding_dispatcher": onboarding_dispatcher.status(),
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }

    status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(content=response, status_code=status_code)
