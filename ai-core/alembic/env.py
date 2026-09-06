"""Alembic environment.

The database URL comes from the application's settings so migrations always
target the same database the running service uses. Every application-owned
model is imported through ``app.models`` so the metadata is complete, and the
tables owned by other systems (the LangGraph checkpointer, mem0) are excluded
from every comparison so Alembic can never try to drop or alter them.
"""

from logging.config import fileConfig

from sqlalchemy import (
    engine_from_config,
    pool,
)
from sqlmodel import SQLModel

import app.models  # noqa: F401  (populates SQLModel.metadata)
from alembic import context
from app.services.database import (
    EXTERNALLY_OWNED_TABLES,
    database_url,
    is_externally_owned,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

target_metadata = SQLModel.metadata

# Tables Alembic must never touch: the LangGraph checkpointer's own schema and
# anything mem0 may create. The list lives next to the engine in
# ``app.services.database`` so it is importable (and unit-tested) without
# executing this file. Reflected tables with these names are ignored so
# autogenerate never proposes dropping them.
EXCLUDED_TABLES = EXTERNALLY_OWNED_TABLES


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: D417
    """Keep externally owned tables out of every migration comparison.

    Args:
        obj: The schema object under consideration.
        name: Its name.
        type_: ``"table"``, ``"column"``, ``"index"`` and so on.
        reflected: Whether it came from the live database.
        compare_to: The metadata object it is being compared with, if any.

    Returns:
        bool: False for excluded tables and anything belonging to them.
    """
    if type_ == "table" and is_externally_owned(name):
        return False
    table = getattr(obj, "table", None)
    if table is not None and is_externally_owned(getattr(table, "name", None)):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
