"""Concurrent startup bootstrap must not race.

``_do_all_startup_work()`` runs once per uvicorn worker (``WEB_WORKERS`` > 1
in production) and once per replica during rolling deploys.  Migrations are
serialized via ``MIGRATION_LOCK_ID``, but every DB bootstrap write that runs
afterwards (built-in seeding, env provisioning, admin upsert, defaults sync)
is a non-atomic check-then-insert flow against unique-constrained tables.
Before the fix, two workers booting together could both pass the existence
check for ``global.assistant`` and the loser crashed the whole background
startup with::

    UniqueViolationError: duplicate key value violates unique constraint
    "ix_intents_name"  DETAIL: Key (name)=(global.assistant) already exists.

leaving the worker alive (liveness 200) but permanently unready
(readiness 503) until the deployment was replaced.

These tests pin the two layers of the fix:

1. ALL bootstrap writes live in ``_bootstrap_platform_data()`` and are only
   ever invoked under ``bootstrap_lock()`` (a session-level Postgres
   advisory lock, same pattern as the migration lock but with a distinct
   lock id).
2. Intent / Vertical creation is race-safe by construction
   (``INSERT ... ON CONFLICT DO NOTHING`` + re-select) in BOTH writers —
   the startup seed pass and the platform-registration sync — because the
   advisory lock alone cannot serialize a booting worker against a live pod
   handling ``POST /platform/register``.
"""

from __future__ import annotations

import inspect

from sqlalchemy.dialects import postgresql

# Every bootstrap pass that performs check-then-insert writes against
# unique-constrained tables.  If a new seed pass is added to
# _do_all_startup_work instead of _bootstrap_platform_data, it runs
# UNSERIALIZED in every worker and reintroduces the boot crash.
_BOOTSTRAP_WRITE_CALLS = [
    "seed_builtin_templates",
    "seed_builtin_agents",
    "seed_builtin_skills",
    "seed_builtin_integrations",
    "seed_builtin_knowledge_bases",
    "seed_builtin_intent_bindings",
    "_seed_llm_providers_from_env",
    "_seed_xtm_one_embedding_server_from_env",
    "_seed_langfuse_from_env",
]


def test_bootstrap_lock_id_is_distinct_from_migration_lock_id():
    """The bootstrap lock MUST NOT reuse the migration lock id — releasing
    the migration lock would otherwise unblock a bootstrap waiter while the
    winner is still seeding."""
    from app.core import startup_locks

    assert startup_locks.BOOTSTRAP_LOCK_ID != startup_locks.MIGRATION_LOCK_ID


def test_bootstrap_lock_takes_session_level_advisory_lock():
    """bootstrap_lock() must use a session-level pg_advisory_lock (held
    across transactions) with statement_timeout disabled so waiters can
    block for as long as the winner needs."""
    from app.core import migrations

    src = inspect.getsource(migrations.bootstrap_lock) + inspect.getsource(migrations._release_startup_lock)
    assert "pg_advisory_lock" in src
    assert "pg_advisory_unlock" in src
    assert "SET statement_timeout = 0" in src
    # Transaction-scoped (xact) locks release on the first commit inside the
    # block and would NOT cover the multi-session bootstrap.
    assert "pg_advisory_xact_lock" not in src


def test_bootstrap_lock_restores_statement_timeout_before_pool_return():
    """Unlike the migration lock (whose connection dies in the
    ``engine.dispose()`` at the end of ``run_migrations``), the bootstrap
    lock's pooled connection goes straight back to the pool.  It must RESET
    the session-level ``statement_timeout = 0`` override in the ``finally``
    block, or the disabled timeout leaks to whatever request checks the
    connection out next (losing the 30 s guard set via asyncpg
    ``server_settings`` in app/core/database.py)."""
    from app.core import migrations

    src = inspect.getsource(migrations.bootstrap_lock) + inspect.getsource(migrations._release_startup_lock)
    assert "RESET statement_timeout" in src
    # The RESET must live in the finally block (after the unlock) so it runs
    # on the failure path too.
    finally_idx = src.find("finally:")
    reset_idx = src.find("RESET statement_timeout")
    assert finally_idx != -1
    assert reset_idx > finally_idx


def test_bootstrap_lock_cleans_up_when_acquisition_fails():
    """The pre-yield window must be covered too: if ``pg_advisory_lock``
    itself fails (cancelled wait, network error), the finally block still
    has to run the RESET — so the acquisition must live INSIDE the try.
    The finally must also roll back any aborted transaction state before
    issuing session-level statements, and the unlock must be guarded on a
    ``locked`` flag set the moment the SELECT returns (a pooled connection
    silently holding the advisory lock would block every future boot)."""
    from app.core import migrations

    src = inspect.getsource(migrations.bootstrap_lock) + inspect.getsource(migrations._release_startup_lock)
    try_idx = src.find("try:")
    acquire_idx = src.find("pg_advisory_lock(")
    finally_idx = src.find("finally:")
    rollback_idx = src.find("conn.rollback()")
    assert try_idx != -1 and acquire_idx != -1
    assert try_idx < acquire_idx, (
        "pg_advisory_lock must be acquired inside the try block so a failed "
        "acquisition still RESETs statement_timeout in the finally"
    )
    assert rollback_idx > finally_idx != -1, (
        "the finally block must roll back aborted transaction state before "
        "the session-level unlock / RESET statements"
    )
    assert "held=locked" in src and "if held:" in src, (
        "pg_advisory_unlock must be guarded on a locked flag so the cleanup "
        "never claims to unlock a lock it does not hold"
    )


def test_all_bootstrap_writes_run_inside_bootstrap_platform_data():
    """Every seeding / provisioning call must live in
    _bootstrap_platform_data (the function invoked under the lock), and
    none may remain in _do_all_startup_work where it would run
    unserialized in every worker."""
    from app import main

    bootstrap_src = inspect.getsource(main._bootstrap_platform_data)
    startup_src = inspect.getsource(main._do_all_startup_work)

    # The built-in AI content moved into ``services/provisioning.py`` so the
    # runtime AI toggle can create the same thing. The invariant is
    # unchanged — ``_bootstrap_platform_data`` calls it, so it still runs under
    # ``bootstrap_lock()`` — but the calls are one level down, so follow the
    # delegation. Guarded below: if boot stops calling it, this stops counting
    # and every seed call is reported missing again.
    from app.services.provisioning import (
        provision_builtin_content,
        provision_env_providers,
    )

    for delegate in (provision_builtin_content, provision_env_providers):
        name = delegate.__name__
        assert f"{name}()" in bootstrap_src, (
            f"boot no longer calls {name}, so the steps it owns would run "
            "outside bootstrap_lock() — or not at all"
        )
        assert f"{name}(" not in startup_src, (
            "provisioning must not be called from _do_all_startup_work — it "
            "would race across uvicorn workers/replicas"
        )
        bootstrap_src += inspect.getsource(delegate)

    for call in _BOOTSTRAP_WRITE_CALLS:
        assert f"{call}(" in bootstrap_src, (
            f"{call} must run inside _bootstrap_platform_data so it is "
            f"serialized by bootstrap_lock()"
        )
        assert f"{call}(" not in startup_src, (
            f"{call} must NOT be called directly from _do_all_startup_work "
            f"— it would race across uvicorn workers/replicas"
        )


def test_startup_invokes_bootstrap_platform_data_under_the_lock():
    """_do_all_startup_work must wrap _bootstrap_platform_data in
    ``async with bootstrap_lock():``."""
    from app import main

    src = inspect.getsource(main._bootstrap_or_adopt)
    lock_idx = src.find("async with bootstrap_lock():")
    call_idx = src.find("_bootstrap_platform_data()")
    assert lock_idx != -1, "bootstrap_lock() is not acquired during startup"
    assert call_idx != -1, "_bootstrap_platform_data() is not invoked"
    assert lock_idx < call_idx, (
        "_bootstrap_platform_data() must be called INSIDE the "
        "bootstrap_lock() block"
    )


def test_get_or_create_intent_compiles_to_on_conflict_do_nothing():
    """The intent upsert must emit ``INSERT ... ON CONFLICT (name) DO
    NOTHING`` so a losing concurrent insert is silently skipped instead of
    raising UniqueViolationError on ix_intents_name."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.api.v1.helpers import get_or_create_intent
    from app.models.intent import Intent

    src = inspect.getsource(get_or_create_intent)
    assert "on_conflict_do_nothing" in src

    # The exact statement shape the helper builds.
    stmt = (
        pg_insert(Intent)
        .values(name="global.assistant", description="x")
        .on_conflict_do_nothing(index_elements=["name"])
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (name) DO NOTHING" in sql


def test_get_or_create_vertical_uses_on_conflict_do_nothing():
    from app.api.v1.helpers import get_or_create_vertical

    src = inspect.getsource(get_or_create_vertical)
    assert "on_conflict_do_nothing" in src


def test_get_or_create_helpers_derive_created_from_insert_rowcount():
    """The ``created`` flag must come from the ON CONFLICT insert's rowcount
    (``INSERT 0 0`` when the conflict skipped) — a loser that raced a
    concurrent creator must report ``created=False``, not claim the row it
    didn't insert."""
    from app.api.v1.helpers import get_or_create_intent, get_or_create_vertical

    for helper in (get_or_create_intent, get_or_create_vertical):
        src = inspect.getsource(helper)
        assert "rowcount" in src, (
            f"{helper.__name__} must derive `created` from the insert "
            f"rowcount so racing losers don't report created=True"
        )


def test_seed_intent_bindings_uses_race_safe_upserts():
    """The seeder must create Intent / Vertical rows through the race-safe
    helpers — never via direct ``db.add(Intent(...))`` check-then-insert."""
    from app.agents import seed

    src = inspect.getsource(seed.seed_builtin_intent_bindings)
    assert "get_or_create_intent" in src
    assert "get_or_create_vertical" in src
    assert "Intent(" not in src.replace("get_or_create_intent(", "").replace(
        "IntentBinding(", ""
    ), "direct Intent(...) construction reintroduces the race"
    assert "Vertical(" not in src.replace("get_or_create_vertical(", "")


def test_platform_registration_uses_race_safe_upserts():
    """Platform registration writes intents/verticals concurrently with the
    startup seed pass (a live pod vs a booting one), so it must use the
    same race-safe helpers and an ON CONFLICT declaration insert."""
    from app.api.v1 import platform

    src = inspect.getsource(platform._sync_platform_intents)
    assert "get_or_create_intent" in src
    assert "get_or_create_vertical" in src
    assert "on_conflict_do_nothing" in src
    assert "Intent(" not in src.replace("get_or_create_intent(", "").replace(
        "PlatformIntentDeclaration(", ""
    ), "direct Intent(...) construction reintroduces the race"
    assert "Vertical(" not in src.replace("get_or_create_vertical(", "")


class _Connection:
    """Stands in for the lock's connection; ``closed`` is a failover closing it."""

    def __init__(self) -> None:
        self.closed = False
        self.invalidated = False
        self.broken_cleanup = False

    async def _use(self) -> None:
        if self.closed:
            from sqlalchemy import exc as sa_exc

            self.invalidated = True
            raise sa_exc.DBAPIError("SELECT 1", {}, Exception("connection is closed"), connection_invalidated=True)

    async def execute(self, statement, params=None) -> None:
        await self._use()
        if self.broken_cleanup and "unlock" in str(statement):
            raise RuntimeError("unlock failed on a live connection")

    async def commit(self) -> None:
        await self._use()

    async def rollback(self) -> None:
        await self._use()


def _engine_with(monkeypatch, conn: _Connection) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.core import migrations

    @asynccontextmanager
    async def connect():
        yield conn

    monkeypatch.setattr(migrations, "engine", SimpleNamespace(connect=connect))


async def test_a_lost_lock_connection_does_not_hide_the_bodys_error(monkeypatch):
    """The failover closed the lock's connection too: the cleanup fails on it,
    and used to raise ``connection is closed`` from ``pg_advisory_unlock`` in
    place of the error that broke the bootstrap (#10368)."""
    import pytest

    from app.core.migrations import bootstrap_lock

    conn = _Connection()
    _engine_with(monkeypatch, conn)

    with pytest.raises(ValueError, match="the bootstrap's own error"):
        async with bootstrap_lock():
            conn.closed = True
            raise ValueError("the bootstrap's own error")


async def test_a_bootstrap_that_finished_stands_when_the_lock_connection_is_lost(monkeypatch):
    from app.core.migrations import bootstrap_lock

    conn = _Connection()
    _engine_with(monkeypatch, conn)

    async with bootstrap_lock():
        conn.closed = True


async def test_a_cleanup_failure_on_a_live_connection_is_still_raised(monkeypatch):
    """A live connection whose unlock failed may still hold the lock: never swallowed."""
    import pytest

    from app.core.migrations import bootstrap_lock

    conn = _Connection()
    conn.broken_cleanup = True
    _engine_with(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="unlock failed"):
        async with bootstrap_lock():
            pass
