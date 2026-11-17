"""One throwaway PostgreSQL database for an integration test, and nothing left behind.

A PostgreSQL-backed suite creates a database of its own from ``DATABASE_URL``
(the role needs CREATEDB), builds the schema it needs there, and drops it
afterwards. Every such module used to carry its own copy of that recipe, and
the copies drifted: some dropped the database without terminating what was
still attached to it, and only a few pointed the app's own session factories at
it, so code under test that opens a session of its own reached
``DATABASE_URL``'s database - one no test migrated - through a pool that
outlived the test's event loop.

``scratch_database`` is the one copy. Whatever the body does, failing tests and
skips included, on the way out it restores what it rebound, disposes its
engine, terminates every connection still attached to the scratch database and
drops it. What legitimately differs between suites is a parameter, stated at
the call site:

* ``tables``: ``ALL_TABLES`` (every table registered on ``Base.metadata`` at
  that moment - a module wanting every model imports ``app.models`` itself), a
  list of tables, or ``()`` for an empty database;
* ``extensions``: created before the schema (``"vector"`` for a schema holding
  ``embeddings``); ``skip_without_extensions`` skips instead of failing when
  one is unavailable;
* ``skip_when_unreachable``: skip (the default) or fail when PostgreSQL cannot
  be reached. A role without CREATEDB always skips;
* ``bind_app_sessions``: bind the process-wide session factories to the scratch
  engine for the duration (the default);
* ``stub_worker_queue``: stand a recording stub in for ``app.worker.queue`` for
  the duration;
* ``refuse_other_databases``: while the body runs, refuse every new connection
  to another database - whichever engine or module opens it, the process-wide
  engine included - and fail the test on any, even one the code under test
  swallowed.

The scope is the caller's: ``async with`` in a test body is one database per
test, a module-scoped fixture around it one per module. ``scratch_copy`` copies
a scratch database as it stands, so two code paths can run on the same rows.

A schema is built once per test process: the first database asking for a given
set of tables and extensions runs ``create_all`` into a template database, and
every database after it is a copy of that template (``CREATE DATABASE ...
TEMPLATE``) - the same schema, created by the same ``create_all``, a file copy
instead of hundreds of DDL statements. ``drop_templates()`` removes them when
the session ends (``conftest.py``).
"""

from __future__ import annotations

import contextlib
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Table, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import database
from app.core.database import Base

DEFAULT_DATABASE_URL = "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"

# Captured at import, before any test can replace the module attribute: the
# factory OBJECT is what the app's modules imported by name, so it is the one
# rebound and restored.
_APP_SESSION_FACTORY = database.async_session


class AllTables:
    """Marker for "every table registered on ``Base.metadata``"."""

    def __repr__(self) -> str:
        return "ALL_TABLES"


ALL_TABLES = AllTables()


class RecordingQueue:
    """Stands in for the worker's SAQ queue (``app.worker.queue``).

    The real queue is created at import and its Redis connections belong to the
    event loop that first used them, while every test runs on a fresh loop.
    Enqueuing a job is the worker's business, not the test's: it is recorded.
    """

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any, **_kwargs: Any) -> Any:
        self.jobs.append(job)
        return job


@dataclass(frozen=True)
class ScratchDatabase:
    name: str
    url: str
    engine: AsyncEngine
    sessions: async_sessionmaker
    worker_queue: RecordingQueue | None = None


# pg_locks spans the whole cluster, where other test workers' databases wait on
# locks of their own, and a row-lock wait is a ``transactionid`` lock with no
# database: a waiter is told apart by the database its backend is connected to.
# pg_stat_activity is read once per transaction, so poll it in a fresh one each time.
LOCK_WAITERS_IN_THIS_DATABASE = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE NOT l.granted AND a.datname = current_database()"
)


def database_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value.
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL).strip()


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)


@contextlib.contextmanager
def app_sessions_bound_to(engine: AsyncEngine) -> Iterator[None]:
    """Point the process-wide session factories at *engine*, then put them back.

    Code under test opens sessions of its own - the licence read behind the AI
    gate, the embedding pre-check, the work-item sweep, the trace sink - on the
    app's factory or on the trace sink's cached one. Left alone they reach
    ``DATABASE_URL``'s database, through a pool whose connections outlive the
    test's event loop. ``configure(bind=...)`` rebinds each factory OBJECT in
    place, so every module that imported it by name follows, and the previous
    bind is put back on that same object whatever a test did to the module
    attribute meanwhile. The AI gate's caches are dropped on both sides: an
    answer read from another database must not decide this test.
    """
    from app.services import agentic_gate, ai_state
    from app.services.tracing import local_sink

    factories = (_APP_SESSION_FACTORY, local_sink._build_tracing_session_factory())
    previous_binds = [factory.kw.get("bind") for factory in factories]
    for factory in factories:
        factory.configure(bind=engine)
    agentic_gate.invalidate_cache()
    ai_state.invalidate_cache()
    try:
        yield
    finally:
        for factory, bind in zip(factories, previous_binds, strict=True):
            factory.configure(bind=bind)
        agentic_gate.invalidate_cache()
        ai_state.invalidate_cache()


@contextlib.contextmanager
def worker_queue_stubbed() -> Iterator[RecordingQueue]:
    """Replace ``app.worker.queue`` by a :class:`RecordingQueue`, then restore it.

    The app imports the queue at call time (``from app.worker import queue``
    inside the function), so the module attribute is the one to replace.
    """
    import app.worker as worker

    previous = worker.queue
    stub = RecordingQueue()
    worker.queue = stub
    try:
        yield stub
    finally:
        worker.queue = previous


class OtherDatabaseRefusedError(RuntimeError):
    """A connection to another database, opened while a scratch database is bound."""


@contextlib.contextmanager
def other_databases_refused(name: str) -> Iterator[list[str]]:
    """Refuse every new connection to a database other than *name*; fail on any.

    Rebinding the session factories covers code that opens a session, not code
    holding an ENGINE: ``bootstrap_lock`` takes its advisory lock on the
    process-wide engine itself, and whatever does the same next would reach
    ``DATABASE_URL``'s database - on a developer machine, the live one.
    SQLAlchemy calls ``do_connect`` for every engine of the process before the
    driver opens a socket, so this refuses at the one point they all share.
    Each refusal is recorded with the app frame that asked, and leaving the
    block fails the test on the record, not on the exception: code under test
    may swallow it (the embedding pre-check logs it and carries on), and a body
    that raised is failed too, chained to its error, so a caller expecting that
    error cannot absorb the refusal.
    """
    refused: list[str] = []

    def _refuse(dialect, connection_record, cargs, cparams):
        target = cparams.get("database")
        if target == name:
            return None
        refused.append(f"{target!r} from {database._first_app_frame()}")
        raise OtherDatabaseRefusedError(
            f"connection to database {target!r} refused while {name!r} is bound"
        )

    def _fail_on_refusals() -> None:
        if refused:
            pytest.fail(f"Refused while {name!r} was bound: {'; '.join(refused)}")

    event.listen(Engine, "do_connect", _refuse)
    try:
        yield refused
    except Exception:
        _fail_on_refusals()
        raise
    finally:
        event.remove(Engine, "do_connect", _refuse)
    _fail_on_refusals()


async def _create_database(
    admin_url: str, name: str, *, skip_when_unreachable: bool, template: str | None = None
) -> None:
    statement = f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(statement))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        if _sqlstate(exc) == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:
        if not skip_when_unreachable:
            raise
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin.dispose()


async def _drop_database(admin_url: str, name: str) -> None:
    cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with cleanup.connect() as conn:
            # Whatever is still attached - an engine a test built on the URL, a
            # task the code under test left running - would make the DROP fail
            # and leave the database behind.
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await cleanup.dispose()


def with_referenced_tables(tables: Sequence[Table]) -> list[Table]:
    """*tables* and every table they reference by a foreign key, transitively, in a stable order.

    A schema with ``users`` holds the tables ``users`` points at: its deferred
    (``use_alter``) key to ``custom_dashboards`` is added after the tables are
    created, and fails when the referenced table is not there.
    """
    selected: dict[str, Table] = {table.name: table for table in tables}
    stack = list(tables)
    while stack:
        for constraint in stack.pop().foreign_key_constraints:
            referred = constraint.referred_table
            if referred.name not in selected:
                selected[referred.name] = referred
                stack.append(referred)
    return [table for table in Base.metadata.sorted_tables if table.name in selected]


def with_request_tables(tables: Sequence[Table]) -> list[Table]:
    """*tables*, and - when they hold ``users`` - the tables every authenticated request reads: the
    companion's PIN lock (``deps.get_current_user`` asks ``app_lock`` on every request of a session)."""
    from app.models.app_lock import AppLockPin, AppLockSession

    chosen = list(tables)
    names = {table.name for table in chosen}
    if "users" in names:
        chosen += [table for table in (AppLockPin.__table__, AppLockSession.__table__) if table.name not in names]
    return chosen


async def _create_schema(
    engine: AsyncEngine,
    tables: Sequence[Table] | AllTables,
    extensions: Sequence[str],
    *,
    skip_without_extensions: bool,
) -> None:
    if not extensions and not isinstance(tables, AllTables) and not tables:
        return
    async with engine.begin() as conn:
        for extension in extensions:
            try:
                await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
            except Exception as exc:
                if not skip_without_extensions:
                    raise
                pytest.skip(f"PostgreSQL extension {extension!r} unavailable: {exc}")
        if isinstance(tables, AllTables):
            await conn.run_sync(Base.metadata.create_all)
        elif tables:
            await conn.run_sync(Base.metadata.create_all, tables=with_referenced_tables(with_request_tables(tables)))


#: ``(admin URL, schema key)`` -> the template holding that schema, this process.
_TEMPLATES: dict[tuple[str, tuple[Any, ...]], str] = {}
#: Every template this process created, for ``drop_templates``.
_CREATED_TEMPLATES: list[tuple[str, str]] = []


def _schema_key(tables: Sequence[Table] | AllTables, extensions: Sequence[str]) -> tuple[Any, ...]:
    """What ``_create_schema`` would build, as a key: the tables it would be
    handed at this moment (``ALL_TABLES`` grows as modules register models)
    and the extensions created before them."""
    selected = (
        Base.metadata.sorted_tables if isinstance(tables, AllTables) else with_referenced_tables(with_request_tables(tables))
    )
    return (
        tuple(extensions),
        tuple(
            (table.name, id(table), len(table.columns), len(table.indexes), len(table.constraints))
            for table in selected
        ),
    )


async def _template_for(
    parsed: urllib.parse.ParseResult,
    admin_url: str,
    tables: Sequence[Table] | AllTables,
    extensions: Sequence[str],
    *,
    skip_without_extensions: bool,
    skip_when_unreachable: bool,
) -> str | None:
    """The template database holding this schema, built on first use."""
    if not extensions and not isinstance(tables, AllTables) and not tables:
        return None
    key = (admin_url, _schema_key(tables, extensions))
    if (name := _TEMPLATES.get(key)) is not None:
        return name
    name = f"scratch_template_{uuid.uuid4().hex[:12]}"
    await _create_database(admin_url, name, skip_when_unreachable=skip_when_unreachable)
    # NullPool: a template may not be copied while a connection is attached.
    engine = create_async_engine(parsed._replace(path=f"/{name}").geturl(), poolclass=NullPool)
    try:
        await _create_schema(engine, tables, extensions, skip_without_extensions=skip_without_extensions)
    except BaseException:
        await engine.dispose()
        await _drop_database(admin_url, name)
        raise
    await engine.dispose()
    _CREATED_TEMPLATES.append((admin_url, name))
    _TEMPLATES[key] = name
    return name


def drop_templates() -> None:
    """Drop every template this process created (the end of the session)."""
    import asyncio

    created = list(_CREATED_TEMPLATES)
    _CREATED_TEMPLATES.clear()
    _TEMPLATES.clear()

    async def _drop_all() -> None:
        for admin_url, name in created:
            with contextlib.suppress(Exception):
                await _drop_database(admin_url, name)

    if created:
        asyncio.run(_drop_all())


@asynccontextmanager
async def scratch_database(
    prefix: str,
    *,
    tables: Sequence[Table] | AllTables,
    extensions: Sequence[str] = (),
    skip_without_extensions: bool = False,
    skip_when_unreachable: bool = True,
    bind_app_sessions: bool = True,
    stub_worker_queue: bool = False,
    refuse_other_databases: bool = False,
    base_url: str | None = None,
) -> AsyncIterator[ScratchDatabase]:
    """Create ``<prefix>_<random>`` next to ``base_url`` (default: ``DATABASE_URL``).

    Dropped on exit, after its connections are terminated, whether the body
    returned, failed or skipped.
    """
    parsed = urllib.parse.urlparse(base_url or database_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    url = parsed._replace(path=f"/{name}").geturl()

    template = await _template_for(
        parsed,
        admin_url,
        tables,
        extensions,
        skip_without_extensions=skip_without_extensions,
        skip_when_unreachable=skip_when_unreachable,
    )
    await _create_database(
        admin_url, name, skip_when_unreachable=skip_when_unreachable, template=template
    )
    try:
        engine = create_async_engine(url)
        try:
            with contextlib.ExitStack() as stack:
                if bind_app_sessions:
                    stack.enter_context(app_sessions_bound_to(engine))
                queue = stack.enter_context(worker_queue_stubbed()) if stub_worker_queue else None
                if refuse_other_databases:
                    stack.enter_context(other_databases_refused(name))
                yield ScratchDatabase(
                    name=name,
                    url=url,
                    engine=engine,
                    sessions=async_sessionmaker(engine, expire_on_commit=False),
                    worker_queue=queue,
                )
        finally:
            await engine.dispose()
    finally:
        await _drop_database(admin_url, name)


@asynccontextmanager
async def scratch_copy(source: ScratchDatabase, *, bind_app_sessions: bool = True) -> AsyncIterator[ScratchDatabase]:
    """A copy of *source* as it is now (``CREATE DATABASE ... TEMPLATE``), dropped on exit.

    Two code paths run on the same rows - every default a seed did not set
    included - so a test can compare what each left behind. The source's pooled
    connections are closed first, since a database cannot be copied while one is
    attached; it reconnects on its next use.
    """
    parsed = urllib.parse.urlparse(source.url)
    admin_url = parsed._replace(path="/postgres").geturl()
    name = f"{source.name}_copy"
    url = parsed._replace(path=f"/{name}").geturl()
    await source.engine.dispose()
    await _create_database(admin_url, name, skip_when_unreachable=False, template=source.name)
    try:
        engine = create_async_engine(url)
        try:
            with contextlib.ExitStack() as stack:
                if bind_app_sessions:
                    stack.enter_context(app_sessions_bound_to(engine))
                yield ScratchDatabase(
                    name=name,
                    url=url,
                    engine=engine,
                    sessions=async_sessionmaker(engine, expire_on_commit=False),
                )
        finally:
            await engine.dispose()
    finally:
        await _drop_database(admin_url, name)
