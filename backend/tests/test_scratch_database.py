"""The shared scratch-database helper keeps its promises.

Every PostgreSQL-backed suite relies on ``tests/scratch_database.py`` to hand
code under test the scratch database - including code that opens a session of
its own - and to leave nothing behind, whatever the test did. Those promises
are pinned here, against a real server; skipped when there is none.
"""

from __future__ import annotations

import contextlib
import urllib.parse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import database
from app.models.user import User
from app.services.tracing import local_sink
from tests.scratch_database import (
    ALL_TABLES,
    OtherDatabaseRefusedError,
    database_url,
    scratch_database,
)

pytestmark = pytest.mark.integration


class _BodyError(Exception):
    pass


async def _current_database(factory) -> str:
    async with factory() as session:
        return (await session.execute(text("SELECT current_database()"))).scalar_one()


async def _database_exists(name: str) -> bool:
    admin = create_async_engine(
        urllib.parse.urlparse(database_url())._replace(path="/postgres").geturl(),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            found = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            return found.scalar() is not None
    finally:
        await admin.dispose()


async def test_code_opening_its_own_session_reaches_the_scratch_database(monkeypatch):
    app_factory = database.async_session
    traces_factory = local_sink._build_tracing_session_factory()
    traces_bind = traces_factory.kw.get("bind")
    with monkeypatch.context() as patch:
        async with scratch_database("scratchpin", tables=[User.__table__]) as db:
            assert await _current_database(database.async_session) == db.name
            assert await _current_database(traces_factory) == db.name
            # A test replacing the module attribute must not redirect the
            # restore: the factory OBJECT is what gets bound back.
            patch.setattr(database, "async_session", db.sessions)
    assert app_factory.kw["bind"] is database.engine
    assert traces_factory.kw.get("bind") is traces_bind


async def test_a_failing_test_still_restores_drops_and_terminates():
    stray = None
    with pytest.raises(_BodyError):
        async with scratch_database("scratchfail", tables=ALL_TABLES, extensions=("vector",)) as db:
            name = db.name
            # An engine the test built on the URL and never disposed.
            stray = create_async_engine(db.url)
            async with stray.connect() as conn:
                await conn.execute(text("SELECT 1"))
            raise _BodyError
    try:
        assert database.async_session.kw["bind"] is database.engine
        assert not await _database_exists(name)
    finally:
        await stray.dispose()


async def test_an_empty_database_holds_no_table_and_leaves_the_app_alone():
    async with scratch_database("scratchempty", tables=(), bind_app_sessions=False) as db:
        assert database.async_session.kw["bind"] is database.engine
        async with db.engine.connect() as conn:
            tables = await conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
            )
            assert tables.scalar_one() == 0
    assert not await _database_exists(db.name)


async def test_the_worker_queue_stub_is_restored():
    import app.worker as worker

    real = worker.queue
    async with scratch_database("scratchqueue", tables=(), stub_worker_queue=True) as db:
        assert worker.queue is db.worker_queue
        await worker.queue.enqueue("job")
        assert db.worker_queue.jobs == ["job"]
    assert worker.queue is real


@pytest.mark.parametrize("body_raises", [False, True])
async def test_another_database_is_refused_and_fails_the_test_even_when_swallowed(body_raises):
    elsewhere = urllib.parse.urlparse(database_url())._replace(path="/postgres").geturl()
    # A caller expecting the body's own error must not absorb the refusal.
    expected = pytest.raises(_BodyError) if body_raises else contextlib.nullcontext()
    with pytest.raises(pytest.fail.Exception, match="'postgres'"), expected:
        async with scratch_database(
            "scratchrefuse", tables=(), refuse_other_databases=True
        ) as db:
            name = db.name
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            stray = create_async_engine(elsewhere)
            try:
                with contextlib.suppress(OtherDatabaseRefusedError):
                    async with stray.connect():
                        pass
            finally:
                await stray.dispose()
            if body_raises:
                raise _BodyError
    # _database_exists connects to "postgres": the refusal ended with the body.
    assert not await _database_exists(name)


async def test_an_unreachable_server_skips_or_fails_as_asked():
    nowhere = "postgresql+asyncpg://copilot:copilot@127.0.0.1:1/copilot"
    with pytest.raises(pytest.skip.Exception):
        async with scratch_database("scratchnone", tables=(), base_url=nowhere):
            pass
    with pytest.raises(OSError):
        async with scratch_database(
            "scratchnone", tables=(), base_url=nowhere, skip_when_unreachable=False
        ):
            pass
