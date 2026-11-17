"""``run_migrations`` under a PostgreSQL failover (#10368).

A dropped ``alembic_version`` read used to count as a fresh database and stamp
head over an existing schema, and the migration lock's cleanup on a dead
connection used to raise over the error that ended the locked block.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import exc as sa_exc

from app.core import migrations


class _PostgresError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _connection_lost() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError("SELECT 1", {}, Exception("connection is closed"), connection_invalidated=True)


class _Connection:
    """The migration lock's connection; ``closed`` is a failover closing it."""

    def __init__(self, revision_read: BaseException | str | None) -> None:
        self.revision_read = revision_read
        self.closed = False
        self.invalidated = False

    async def _use(self) -> None:
        if self.closed:
            self.invalidated = True
            raise _connection_lost()

    async def execute(self, statement, params=None):
        await self._use()
        if "alembic_version" in str(statement) and isinstance(self.revision_read, BaseException):
            raise self.revision_read
        revision = self.revision_read if isinstance(self.revision_read, str) else None
        return SimpleNamespace(scalar=lambda: True, scalar_one_or_none=lambda: revision)

    async def commit(self) -> None:
        await self._use()

    async def rollback(self) -> None:
        await self._use()


@pytest.fixture
def migration(monkeypatch):
    """Runs ``run_migrations`` on a fake connection and records the schema steps."""
    steps: list[str] = []

    def install(conn: _Connection, *, on_create_tables=None) -> list[str]:
        @asynccontextmanager
        async def connect():
            yield conn

        async def dispose() -> None:
            steps.append("dispose")

        def record(name: str, action=None):
            async def step(*_args) -> None:
                steps.append(name)
                if action is not None:
                    action()

            return step

        monkeypatch.setattr(migrations, "engine", SimpleNamespace(connect=connect, dispose=dispose))
        monkeypatch.setattr(migrations, "_reconcile_pre_squash_revision", record("reconcile"))
        monkeypatch.setattr(migrations, "_create_missing_tables", record("create_all", on_create_tables))
        monkeypatch.setattr(migrations, "_apply_pending_migrations", record("upgrade"))
        monkeypatch.setattr(migrations, "_stamp_head", record("stamp"))
        for name in ("_ensure_model_columns", "_ensure_schema_extras", "_ensure_model_indexes"):
            monkeypatch.setattr(migrations, name, record(name))
        return steps

    return install


async def test_a_missing_alembic_version_table_is_a_fresh_database(migration):
    missing = sa_exc.ProgrammingError("SELECT", {}, _PostgresError("42P01"))
    steps = migration(_Connection(missing))

    await migrations.run_migrations()

    assert "stamp" in steps and "upgrade" not in steps


async def test_a_lost_alembic_version_read_never_stamps_head_over_a_schema(migration):
    """The failover dropped the read: an existing database must not be marked current."""
    steps = migration(_Connection(_connection_lost()))

    with pytest.raises(sa_exc.DBAPIError):
        await migrations.run_migrations()

    assert steps == []


async def test_an_existing_database_is_upgraded(migration):
    steps = migration(_Connection("0123abcd"))

    await migrations.run_migrations()

    assert "upgrade" in steps and "stamp" not in steps


async def test_a_lost_migration_lock_connection_does_not_hide_the_migrations_error(migration):
    conn = _Connection(None)

    def fail_after_the_failover() -> None:
        conn.closed = True
        raise ValueError("the migration's own error")

    migration(conn, on_create_tables=fail_after_the_failover)

    with pytest.raises(ValueError, match="the migration's own error"):
        await migrations.run_migrations()
