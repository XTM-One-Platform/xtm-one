"""The ``e4059embfk01`` revision, executed against PostgreSQL.

The model no longer declares the foreign key, so a scratch schema built from it
has none: the constraint is added back the way the baseline created it
(unnamed, ``ON DELETE SET NULL``), then the revision runs both ways. The
revision is loaded from its file: a migration must keep meaning what it meant
on the day it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401 - registers every table
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "e4059embfk01_drop_embeddings_provider_fk.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("e4059embfk01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()

_PROVIDER_FKS = text(
    "SELECT con.conname, con.convalidated, con.confdeltype::text FROM pg_constraint con "
    "JOIN pg_class rel ON rel.oid = con.conrelid "
    "JOIN pg_class ref ON ref.oid = con.confrelid "
    "WHERE con.contype = 'f' AND rel.relname = 'embeddings' "
    "AND ref.relname = 'llm_provider_configs'"
)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[AsyncEngine]:
    async with scratch_database(
        "embfk", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.engine.begin() as conn:
            # As the baseline created it: unnamed, so PostgreSQL named it.
            await conn.execute(
                text(
                    "ALTER TABLE embeddings ADD FOREIGN KEY (embedding_provider_id) "
                    "REFERENCES llm_provider_configs (id) ON DELETE SET NULL"
                )
            )
        yield db.engine


async def _run_revision(engine: AsyncEngine, step) -> None:
    def _call(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            step()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _provider_fks(engine: AsyncEngine) -> list[tuple[str, bool, str]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(_PROVIDER_FKS)).all()]


@pytest.mark.asyncio
async def test_upgrade_drops_the_baseline_constraint_and_downgrade_restores_it_not_valid():
    async with _temp_database() as engine:
        assert len(await _provider_fks(engine)) == 1

        await _run_revision(engine, revision.upgrade)
        assert await _provider_fks(engine) == []
        # Re-run: nothing left to drop, no error.
        await _run_revision(engine, revision.upgrade)
        assert await _provider_fks(engine) == []

        # A chunk still pointing at a config deleted before the sweep ran.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO embeddings "
                    "(id, source_type, embedding_provider_id, content, embedding, created_at) "
                    "VALUES (:id, 'document', :provider, 'orphan', '[0.1,0.2,0.3]', now())"
                ),
                {"id": uuid.uuid4(), "provider": uuid.uuid4()},
            )

        # NOT VALID: the dangling id does not fail the downgrade.
        await _run_revision(engine, revision.downgrade)
        assert await _provider_fks(engine) == [(revision._CONSTRAINT, False, "n")]
        await _run_revision(engine, revision.downgrade)
        assert await _provider_fks(engine) == [(revision._CONSTRAINT, False, "n")]

        await _run_revision(engine, revision.upgrade)
        assert await _provider_fks(engine) == []


@pytest.mark.asyncio
async def test_the_drop_locks_both_tables_the_pre_flight_clears():
    """Dropping the key removes RI triggers on both tables: ACCESS EXCLUSIVE on each."""
    async with _temp_database() as engine:

        def _upgrade_and_read_locks(sync_conn) -> set[str]:
            with Operations.context(MigrationContext.configure(sync_conn)):
                revision.upgrade()
            rows = sync_conn.execute(
                text(
                    "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.pid = pg_backend_pid() AND l.mode = 'AccessExclusiveLock' "
                    "AND c.relname IN ('embeddings', 'llm_provider_configs')"
                )
            ).scalars()
            return set(rows)

        async with engine.begin() as conn:
            locked = await conn.run_sync(_upgrade_and_read_locks)
        assert locked == {"embeddings", "llm_provider_configs"}
