"""What the startup watchdog reads from PostgreSQL, and the Alembic bounds.

The watchdog's report and the Alembic connection's timeouts only mean anything
against a real server: who holds and who waits for the migration advisory lock
is in ``pg_locks``, and whether a ``RESET`` in a migration body keeps the bound
depends on how the bound was set. Whether a replica waiting for that lock holds
a snapshot only shows against a real server too.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
import sqlalchemy.ext.asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import settings
from app.core.db_availability import retrying_connect
from app.core.migrations import _build_alembic_config, alembic_server_settings
from app.core.schema_readiness import read_migration_in_progress
from app.core.startup_locks import MIGRATION_APPLICATION_NAME, MIGRATION_LOCK_ID
from app.core.startup_watchdog import describe_startup_sessions
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration


async def test_the_report_names_the_lock_holder_and_the_sessions_queued_behind_it():
    async with scratch_database("watchdog", tables=(), bind_app_sessions=False) as db:
        assert await describe_startup_sessions(db.engine) == "No database session holds or waits for a startup lock."
        async with db.engine.connect() as holder, db.engine.connect() as waiter:
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar()
            await holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            waiting = asyncio.create_task(
                waiter.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            )
            try:
                report = ""
                for _ in range(50):
                    report = await describe_startup_sessions(db.engine)
                    if f"blocked by [{holder_pid}]" in report:
                        break
                    await asyncio.sleep(0.1)
                assert f"pid {holder_pid} " in report
                assert f"blocked by [{holder_pid}]" in report
                assert "waiting on Lock:advisory" in report
            finally:
                await holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
                await asyncio.wait_for(waiting, 10)
                await waiter.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})


async def test_the_alembic_connection_is_bounded_and_a_reset_keeps_the_bound(monkeypatch):
    monkeypatch.setattr(settings, "migration_lock_timeout_seconds", 300)
    monkeypatch.setattr(settings, "migration_statement_timeout_seconds", 1800)
    async with scratch_database("alembic_bounds", tables=(), bind_app_sessions=False) as db:
        engine = create_async_engine(
            db.url,
            poolclass=NullPool,
            connect_args={"server_settings": alembic_server_settings()},
        )
        try:
            async with engine.connect() as conn:

                async def show(name: str) -> str:
                    return (await conn.execute(text(f"SHOW {name}"))).scalar()

                assert await show("application_name") == MIGRATION_APPLICATION_NAME
                assert await show("lock_timeout") == "5min"
                assert await show("statement_timeout") == "30min"
                # What create_index_in_place does around its build.
                await conn.execute(text("SET statement_timeout = 0"))
                await conn.execute(text("RESET statement_timeout"))
                assert await show("statement_timeout") == "30min"
        finally:
            await engine.dispose()


async def test_the_worker_sees_whether_a_platform_is_migrating():
    async with scratch_database("migrating", tables=(), bind_app_sessions=False) as db:
        assert await read_migration_in_progress(db.engine) is False
        async with db.engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            try:
                assert await read_migration_in_progress(db.engine) is True
            finally:
                await holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})


async def test_alembic_env_opens_its_connection_with_the_bounds(monkeypatch):
    """Through ``alembic/env.py`` itself, as the startup runner drives it."""
    opened: list[dict] = []
    real = sqlalchemy.ext.asyncio.async_engine_from_config

    def recording(*args, **kwargs):
        opened.append(kwargs.get("connect_args", {}))
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", recording)
    async with scratch_database("alembic_env", tables=(), bind_app_sessions=False) as db:
        monkeypatch.setattr(settings, "database_url", db.url)
        await asyncio.to_thread(command.current, _build_alembic_config())

    # The app engine's retrying connect too: a failover mid-upgrade is waited out (#10368).
    assert opened == [{"server_settings": alembic_server_settings(), "async_creator_fn": retrying_connect}]
    assert opened[0]["server_settings"]["application_name"] == MIGRATION_APPLICATION_NAME
    assert {"lock_timeout", "statement_timeout"} <= opened[0]["server_settings"].keys()


_WAITER_APPLICATION_NAME = "migration-lock-waiter-test"

_HOLDERS_SQL = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND classid = 0 AND objsubid = 1 AND objid = :id "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


async def _migration_lock_waiter(monitor):
    """The session of ``run_migrations``, as ``pg_stat_activity`` shows it."""
    await monitor.execute(text("SELECT pg_stat_clear_snapshot()"))
    return (
        await monitor.execute(
            text(
                "SELECT pid, state, xact_start, backend_xmin FROM pg_stat_activity "
                "WHERE datname = current_database() AND application_name = :name"
            ),
            {"name": _WAITER_APPLICATION_NAME},
        )
    ).one_or_none()


async def test_a_replica_waiting_for_the_migration_lock_holds_no_snapshot(monkeypatch):
    """The lock holder's ``CREATE INDEX CONCURRENTLY`` must not wait for the waiter.

    A replica blocked in ``SELECT pg_advisory_lock(...)`` holds that
    statement's snapshot for the whole wait; a concurrent index build, which
    waits for every older snapshot of the database, then waits for it while it
    waits for the holder. Through ``run_migrations`` itself, its schema steps
    stubbed: while another session holds the migration lock, the waiter shows
    no open transaction and a concurrent build completes; once released, the
    waiter takes the lock, runs every step and releases it.
    """
    from app.core import migrations
    from app.core.startup_watchdog import current_startup_step

    async with scratch_database(
        "migration_lock_wait",
        tables=(),
        extensions=("vector",),
        skip_without_extensions=True,
        bind_app_sessions=False,
    ) as db:
        ran: list[str] = []

        def recorder(name: str):
            async def record(*_args, **_kwargs) -> None:
                ran.append(name)

            return record

        for name in (
            "_reconcile_pre_squash_revision",
            "_create_missing_tables",
            "_apply_pending_migrations",
            "_stamp_head",
            "_ensure_model_columns",
            "_ensure_schema_extras",
            "_ensure_model_indexes",
        ):
            monkeypatch.setattr(migrations, name, recorder(name))
        # Its own engine, so its session is found by name.
        waiter_engine = create_async_engine(
            db.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"application_name": _WAITER_APPLICATION_NAME}},
        )
        monkeypatch.setattr(migrations, "engine", waiter_engine)

        async with db.engine.connect() as setup:
            await setup.execute(text("CREATE TABLE migration_lock_probe (id integer)"))
            await setup.commit()

        migrating: asyncio.Task | None = None
        try:
            async with db.engine.connect() as holder, db.engine.connect() as monitor_conn:
                monitor = await monitor_conn.execution_options(isolation_level="AUTOCOMMIT")
                await holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
                await holder.commit()
                try:
                    migrating = asyncio.create_task(migrations.run_migrations())
                    # Sampled between two attempts, the waiter has no
                    # transaction and no snapshot. It is idle too right after
                    # its ``SET statement_timeout`` commit, before it enters
                    # the waiting step: sample it once in that step.
                    seen: list[str] = []
                    waiter = None
                    for _ in range(100):
                        waiter = await _migration_lock_waiter(monitor)
                        seen.append(f"{waiter.state if waiter else '-'}/{current_startup_step()}")
                        if (
                            waiter is not None
                            and waiter.state == "idle"
                            and current_startup_step() == "waiting for the migration lock"
                        ):
                            break
                        await asyncio.sleep(0.05)
                    assert waiter is not None and waiter.state == "idle", seen
                    assert waiter.xact_start is None
                    assert waiter.backend_xmin is None
                    assert current_startup_step() == "waiting for the migration lock"

                    async with db.engine.connect() as builder_conn:
                        builder = await builder_conn.execution_options(isolation_level="AUTOCOMMIT")
                        # Bounded, so a waiter holding a snapshot fails the test
                        # instead of hanging it.
                        await builder.execute(text("SET lock_timeout = '15s'"))
                        await builder.execute(text("SET statement_timeout = '20s'"))
                        await builder.execute(
                            text("CREATE INDEX CONCURRENTLY ix_migration_lock_probe ON migration_lock_probe (id)")
                        )
                    assert not migrating.done()
                    assert ran == []
                finally:
                    await holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
                    await holder.commit()
            await asyncio.wait_for(migrating, 15)
        finally:
            if migrating is not None and not migrating.done():
                migrating.cancel()
                with contextlib.suppress(BaseException):
                    await migrating
            await waiter_engine.dispose()

        # A fresh database (no ``alembic_version``): stamped, then reconciled.
        assert ran == [
            "_create_missing_tables",
            "_stamp_head",
            "_ensure_model_columns",
            "_ensure_schema_extras",
            "_ensure_model_indexes",
        ]
        async with db.engine.connect() as conn:
            held = (await conn.execute(_HOLDERS_SQL, {"id": MIGRATION_LOCK_ID})).scalar()
        assert held == 0


async def test_a_bounded_bootstrap_lock_wait_times_out_then_takes_the_freed_lock(monkeypatch):
    """The request path's ``bootstrap_lock(wait_timeout=...)`` polls the same way."""
    from app.core import migrations
    from app.core.startup_locks import BOOTSTRAP_LOCK_ID

    async with scratch_database("bootstrap_lock_wait", tables=(), bind_app_sessions=False) as db:
        monkeypatch.setattr(migrations, "engine", db.engine)
        async with db.engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": BOOTSTRAP_LOCK_ID})
            await holder.commit()
            try:
                with pytest.raises(TimeoutError, match="bootstrap lock"):
                    async with migrations.bootstrap_lock(wait_timeout=0.6):
                        pytest.fail("entered a lock another session holds")
            finally:
                await holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": BOOTSTRAP_LOCK_ID})
                await holder.commit()

        async with db.engine.connect() as monitor:
            async with migrations.bootstrap_lock(wait_timeout=5):
                assert (await monitor.execute(_HOLDERS_SQL, {"id": BOOTSTRAP_LOCK_ID})).scalar() == 1
                await monitor.rollback()
            assert (await monitor.execute(_HOLDERS_SQL, {"id": BOOTSTRAP_LOCK_ID})).scalar() == 0
