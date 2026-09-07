"""Async database engine and session factory for the SprintFlow domain tables.

One engine per process, built on psycopg 3's async driver (already a
dependency), so every data-access function is ``async`` and never blocks the
event loop the Mattermost transports run on. Data access lives in
``app.services.domain``; this module only owns the connection.

The LangGraph checkpointer keeps its own ``psycopg_pool`` connection pool in
``app.core.langgraph.graph`` — the two never share a connection.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.logging import logger

# Tables that live in the same database but belong to other systems: the
# LangGraph checkpointer's schema and anything mem0 may create. Alembic's
# ``env.py`` excludes them from every comparison so autogenerate can never
# propose dropping or altering them, and ``make db-reset`` never lists them.
EXTERNALLY_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        "longterm_memory",
        "mem0migrations",
    }
)


def is_externally_owned(table_name: str | None) -> bool:
    """Report whether a table belongs to another system sharing the database.

    Args:
        table_name: Unqualified table name, or None.

    Returns:
        bool: True for the checkpointer and mem0 tables.
    """
    return table_name is not None and table_name in EXTERNALLY_OWNED_TABLES


def database_url(driver: str = "postgresql+psycopg") -> str:
    """Build the SQLAlchemy URL from the repository's settings.

    Args:
        driver: SQLAlchemy dialect+driver prefix. psycopg 3 serves both the
            async engine here and Alembic's sync engine.

    Returns:
        str: A URL with the password percent-encoded.
    """
    return (
        f"{driver}://{quote_plus(settings.POSTGRES_USER)}:{quote_plus(settings.POSTGRES_PASSWORD)}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )


class DatabaseService:
    """Owns the async engine and hands out sessions."""

    def __init__(self) -> None:
        """Create the engine lazily configured from settings. No connection is opened here."""
        self.engine: AsyncEngine = create_async_engine(
            database_url(),
            pool_pre_ping=True,
            pool_size=settings.POSTGRES_POOL_SIZE,
            max_overflow=settings.POSTGRES_MAX_OVERFLOW,
            pool_timeout=30,
            pool_recycle=1800,
        )
        logger.info(
            "database_engine_configured",
            environment=settings.ENVIRONMENT.value,
            pool_size=settings.POSTGRES_POOL_SIZE,
            max_overflow=settings.POSTGRES_MAX_OVERFLOW,
        )

    def session(self) -> AsyncSession:
        """Return a new session; the caller owns commit/rollback/close.

        ``expire_on_commit=False`` so rows returned from a closed session still
        expose every loaded column — no ``DetachedInstanceError`` at the tool
        boundary.

        Returns:
            AsyncSession: An unopened session bound to the engine.
        """
        return AsyncSession(self.engine, expire_on_commit=False)

    async def health_check(self) -> bool:
        """Check connectivity with a trivial query.

        Returns:
            bool: True when the database answered.
        """
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error("database_health_check_failed", error=str(e))
            return False

    async def table_exists(self, table_name: str) -> bool:
        """Report whether a table exists in the public schema.

        Used by the health endpoint so a container whose migrations never ran
        reports degraded instead of healthy.

        Args:
            table_name: Unqualified table name.

        Returns:
            bool: True when the table exists.
        """
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT to_regclass(:name)"), {"name": f"public.{table_name}"})
                return result.scalar() is not None
        except Exception as e:
            logger.exception("database_table_check_failed", table=table_name, error=str(e))
            return False

    async def close(self) -> None:
        """Dispose of the engine's pool on shutdown."""
        await self.engine.dispose()
        logger.info("database_engine_disposed")


database_service = DatabaseService()


@asynccontextmanager
async def session_scope(session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """Provide a session, opening one unless the caller passed one in.

    Data-access functions accept an optional ``session`` so several of them can
    share one transaction; when none is given, this opens a session, commits on
    success and rolls back on error. When one is given, ownership stays with
    the caller and nothing is committed here.

    Args:
        session: An existing session to reuse, or None.

    Yields:
        AsyncSession: The session to use.
    """
    if session is not None:
        yield session
        return

    own = database_service.session()
    try:
        yield own
        await own.commit()
    except BaseException:
        await own.rollback()
        raise
    finally:
        await own.close()
