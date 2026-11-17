"""Alembic migration environment – async PostgreSQL."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import ALL models so Base.metadata is fully populated.
# All model registrations are centralized in app/models/__init__.py.
import app.models

_ = app.models  # side-effect import: registers all models with Base.metadata
from alembic import context
from app.core.config import settings
from app.core.database import Base
from app.core.db_availability import retrying_connect
from app.core.migration_ddl import STARTING_HEADS_ATTRIBUTE
from app.core.migrations import alembic_server_settings

config = context.config
if config.config_file_name is not None and config.attributes.get(
    "configure_logger",
    True,
):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override URL from environment
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Read before the first pending migration moves ``alembic_version``: a
        # contract migration needs the release this run STARTED from, the one
        # the replicas being replaced still run (``applied_before_this_run``).
        config.attributes[STARTING_HEADS_ATTRIBUTE] = tuple(
            context.get_context().get_current_heads()
        )
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Retried and tagged like the app engine's connects, so a failover
        # mid-upgrade is one the startup waits out.
        connect_args={"server_settings": alembic_server_settings(), "async_creator_fn": retrying_connect},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
