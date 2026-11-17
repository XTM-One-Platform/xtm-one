"""Fresh-install migration path test (mirrors production startup).

On a truly fresh database the Alembic history starts *after* the initial
schema was created via ``Base.metadata.create_all``.  This test replicates
the production flow from ``app/core/migrations.py``:

1. ``CREATE EXTENSION IF NOT EXISTS vector`` — pgvector must exist before
   any DDL that references ``VECTOR`` columns.
2. ``Base.metadata.create_all`` — creates all tables from SQLAlchemy models.
3. ``alembic stamp head`` — marks the DB as current (no DDL).
4. Verifies that the resulting DB schema matches ``Base.metadata``.

Schema drift verification covers:
- Tables present in models but missing from the DB.
- Columns present in models but missing from the DB.
- Tables / columns present in the DB but not in the models (extras
  surfaced as warnings, not failures).

It deliberately does **not** verify column types, defaults, indexes, FK
constraints, or check constraints.

Isolation
---------
The test always runs against a **dedicated temporary database** (created
via ``CREATE DATABASE`` and dropped on teardown), so the "fresh install"
semantic is deterministic regardless of test order — in particular, the
``bootstrap`` CI job (which runs ``pytest -m integration``) also picks up
``test_bootstrap.py``, which starts uvicorn and populates the shared
``DATABASE_URL`` database. Without isolation, this drift check would run
against an already-populated DB and could mask regressions in the
``create_all + stamp head`` startup path.

If the configured DB user lacks ``CREATEDB`` privileges, the test is
skipped with a diagnostic instead of silently degrading.

Requires:
    - PostgreSQL 17 + pgvector running (DATABASE_URL env var)
    - The DATABASE_URL user must have ``CREATEDB`` privilege.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from app.core.database import Base
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_ALEMBIC_CMD = [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini"]

# Tables created by Alembic / PostgreSQL that are not declared in our models.
_KNOWN_EXTRA_TABLES: frozenset[str] = frozenset({"alembic_version"})


@pytest.fixture(scope="module")
def base_database_url():
    """Resolve the configured DATABASE_URL.

    This is the URL of the database the rest of the integration suite uses
    (it is **not** modified by this test — we create a sibling DB for our
    own isolation).
    """
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@contextlib.asynccontextmanager
async def _temporary_database(base_database_url: str):
    """Create a dedicated, empty Postgres database and drop it on exit.

    Using a dedicated DB guarantees the "fresh install" semantic regardless
    of what other tests in the same ``pytest -m integration`` run have
    already done to ``base_database_url`` (e.g. ``test_bootstrap.py``
    starts the full app and runs the production migration path against it).
    """
    # Empty on purpose: the fresh install path builds the schema itself, in a
    # subprocess against the URL, so the app's own factories stay where they
    # are. Only a missing CREATEDB skips; genuine failures (server
    # unreachable, wrong credentials) fail loudly instead of being masked as
    # an environment skip.
    async with scratch_database(
        "copilot_fresh_install",
        tables=(),
        skip_when_unreachable=False,
        bind_app_sessions=False,
        base_url=base_database_url,
    ) as db:
        yield db.url


@pytest.fixture(scope="module")
async def fresh_install_database_url(base_database_url):
    """Module-scoped dedicated DB for the schema-drift assertions below."""
    async with _temporary_database(base_database_url) as fresh_url:
        yield fresh_url


@pytest.fixture(scope="module")
async def fresh_db(fresh_install_database_url):
    """Bootstrap the dedicated fresh DB: pgvector + create_all + stamp head."""
    engine = create_async_engine(fresh_install_database_url)

    # Step 1: ensure pgvector extension
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Step 2: create all tables from models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()

    # Step 3: stamp head against the dedicated DB. We override DATABASE_URL
    # only for the subprocess so we never pollute the parent process env
    # (other tests in the same session must keep seeing the shared
    # ``base_database_url``).
    result = subprocess.run(  # noqa: S603
        [*_ALEMBIC_CMD, "stamp", "head"],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": fresh_install_database_url},
    )
    # Alembic (and any Python exception bubbling up through it) writes the
    # useful context to *both* stdout and stderr depending on what failed,
    # so surface both in the assertion message — getting only stderr is a
    # well-known footgun when debugging CI-only failures.
    assert result.returncode == 0, (
        "alembic stamp head failed "
        f"(exit code {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    yield fresh_install_database_url


async def test_no_missing_tables_or_columns(fresh_db):
    """All model-defined tables and columns must exist in the DB after fresh install."""
    engine = create_async_engine(fresh_db)

    async with engine.connect() as conn:

        def _inspect(sync_conn: sa.Connection) -> tuple[list[str], list[str]]:
            inspector = sa.inspect(sync_conn)
            db_tables = set(inspector.get_table_names())
            model_tables = {t.name for t in Base.metadata.sorted_tables}

            missing: list[str] = []
            extras: list[str] = []

            for t in sorted(model_tables - db_tables):
                missing.append(f"Missing table: {t}")

            for t in sorted(db_tables - model_tables - _KNOWN_EXTRA_TABLES):
                extras.append(f"Extra table not in models: {t}")

            for table in Base.metadata.sorted_tables:
                if table.name not in db_tables:
                    continue
                model_cols = {c.name for c in table.columns}
                db_cols = {c["name"] for c in inspector.get_columns(table.name)}
                for col in sorted(model_cols - db_cols):
                    missing.append(f"Missing column: {table.name}.{col}")
                for col in sorted(db_cols - model_cols):
                    extras.append(f"Extra column not in models: {table.name}.{col}")

            return missing, extras

        missing, extras = await conn.run_sync(_inspect)

    await engine.dispose()

    if extras:
        import warnings

        warnings.warn(
            f"{len(extras)} schema extra(s) (informational): {extras}",
            UserWarning,
            stacklevel=1,
        )

    assert not missing, (
        f"{len(missing)} schema drift error(s) found:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


# Generous ceiling for one full production bootstrap (create_all + stamp
# head + schema extras + index reconciliation).  The old deadlock
# hangs FOREVER, so any finite bound catches the regression while leaving
# plenty of headroom for a slow CI runner.
_BOOTSTRAP_DEADLINE_SECONDS = 300

_RUN_MIGRATIONS_SNIPPET = (
    "import asyncio; "
    "from app.core.migrations import run_migrations; "
    "asyncio.run(run_migrations())"
)


async def test_fresh_bootstrap_completes_alongside_open_snapshot_transaction(
    base_database_url,
):
    """The fresh bootstrap must not deadlock.

    During a multi-pod / multi-worker deploy, every sibling uvicorn worker
    sits in ``SELECT pg_advisory_lock(...)`` while one worker runs the
    migrations — so for the whole duration of the bootstrap there is
    always at least one OTHER open transaction holding a snapshot.  The
    old ``CREATE/DROP INDEX CONCURRENTLY`` in
    ``_ensure_schema_extras`` waits for ALL such transactions to finish,
    and they cannot finish until the lock holder releases: a mutual
    deadlock.  In production the liveness probe then SIGTERMs the pod,
    the killed build leaves the index INVALID, and every subsequent boot
    repeats the cycle (CrashLoopBackOff).

    Reproduce the hazard deterministically: hold a REPEATABLE READ
    transaction with a pinned snapshot — exactly what an advisory-lock
    waiter looks like to ``CONCURRENTLY`` DDL (open transaction +
    snapshot, no lock on the target table) — while the production
    ``run_migrations()`` path runs against a fresh database in a
    subprocess.  The old CONCURRENTLY bootstrap hangs forever and trips
    the deadline; the plain-DDL bootstrap completes and leaves both
    knowledge_documents indexes VALID.
    """
    from app.core import migrations as migrations_mod

    async with _temporary_database(base_database_url) as fresh_url:
        # The "second worker": an open transaction that pins a snapshot
        # (txid_current() also assigns a real xid) for the entire
        # bootstrap, like a worker blocked on pg_advisory_lock().
        blocker_engine = create_async_engine(
            fresh_url, isolation_level="REPEATABLE READ"
        )
        blocker = await blocker_engine.connect()
        try:
            await blocker.execute(text("SELECT txid_current()"))

            # Run the production startup path in a subprocess so it uses
            # its own engine bound to the fresh DB (the in-process engine
            # is bound to the shared DATABASE_URL).
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-c", _RUN_MIGRATIONS_SNIPPET],
                    capture_output=True,
                    text=True,
                    cwd=_BACKEND_DIR,
                    env={**os.environ, "DATABASE_URL": fresh_url},
                    timeout=_BOOTSTRAP_DEADLINE_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                pytest.fail(
                    f"run_migrations() did not finish within "
                    f"{_BOOTSTRAP_DEADLINE_SECONDS}s while another open "
                    f"transaction held a snapshot — the bootstrap "
                    f"deadlock is back (CONCURRENTLY DDL waiting on "
                    f"advisory-lock waiters).\n"
                    f"--- stdout ---\n{exc.stdout}\n"
                    f"--- stderr ---\n{exc.stderr}"
                )

            assert result.returncode == 0, (
                f"run_migrations() failed on a fresh DB with a concurrent "
                f"snapshot-holding transaction (exit {result.returncode}).\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
        finally:
            await blocker.rollback()
            await blocker.close()
            await blocker_engine.dispose()

        # Both bootstrap-managed indexes must exist AND be valid —
        # ``indisvalid = false`` is exactly the poisoned state the
        # production crash-loop kept regenerating.
        verify_engine = create_async_engine(fresh_url)
        try:
            async with verify_engine.connect() as conn:
                for index_name, _create, _drop in (
                    migrations_mod._KB_DOCUMENT_CONCURRENT_INDEXES
                ):
                    row = await conn.execute(
                        text(
                            "SELECT i.indisvalid FROM pg_index i "
                            "JOIN pg_class c ON c.oid = i.indexrelid "
                            "WHERE c.relname = :name"
                        ),
                        {"name": index_name},
                    )
                    indisvalid = row.scalar_one_or_none()
                    assert indisvalid is True, (
                        f"{index_name} must exist and be VALID after the "
                        f"bootstrap; got indisvalid={indisvalid!r}"
                    )
        finally:
            await verify_engine.dispose()
