"""The startup ``create_all`` runs under the migrations' own lock safety.

On an existing install ``run_migrations`` calls ``create_all`` BEFORE the
pending migration bodies, so the table of a new model is created there and its
migration only finds it. A table with a foreign key takes a SHARE ROW EXCLUSIVE
lock on the table it references (dev-docs/reference/migrations-safety.md,
rule 5), so ``create_all`` gets the migration body's belt
(``set_lock_timeout``) and the migration runner's retry on lock contention,
instead of waiting for the connection's 30 s ``statement_timeout`` and failing
the startup for good on its 57014.
"""

import inspect
import logging

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.core import migrations
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT


def _driver_error(sqlstate: str, message: str) -> DBAPIError:
    """A driver error the way SQLAlchemy raises it: the SQLSTATE on its cause."""

    class _DriverError(Exception):
        pass

    driver = _DriverError(message)
    driver.sqlstate = sqlstate
    try:
        raise DBAPIError("CREATE TABLE annotations (...)", None, driver) from driver
    except DBAPIError as exc:
        return exc


class _SyncConn:
    def __init__(self, log: list):
        self.log = log

    def execute(self, statement, params=None):
        self.log.append(("execute", str(statement), dict(params or {})))


class _AsyncConn:
    def __init__(self, log: list, outcome: BaseException | None):
        self.log = log
        self.outcome = outcome

    async def run_sync(self, fn):
        fn(_SyncConn(self.log))
        if self.outcome is not None:
            raise self.outcome


class _Engine:
    """``engine.begin()`` yielding one connection per transaction; each
    transaction ends with the next of *outcomes* (``None`` commits)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.log: list = []
        self.transactions = 0

    def begin(self):
        engine = self
        outcome = self.outcomes.pop(0) if self.outcomes else None

        class _Begin:
            async def __aenter__(self):
                engine.transactions += 1
                engine.log.append(("begin", engine.transactions))
                return _AsyncConn(engine.log, outcome)

            async def __aexit__(self, *exc_info):
                return False

        return _Begin()


@pytest.fixture
def fake_create_all(monkeypatch):
    def _create_all(sync_conn):
        sync_conn.log.append(("create_all",))

    monkeypatch.setattr(migrations.Base.metadata, "create_all", _create_all)


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []

    async def _sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(migrations.asyncio, "sleep", _sleep)
    return recorded


def test_run_migrations_creates_the_tables_through_the_bounded_helper():
    source = inspect.getsource(migrations.run_migrations)
    assert "await _create_missing_tables()" in source
    assert "run_sync(Base.metadata.create_all)" not in source, (
        "run_migrations must not call create_all directly: only "
        "_create_missing_tables gives it the lock_timeout belt and the retry"
    )


@pytest.mark.asyncio
async def test_create_all_runs_after_the_belt_in_the_same_transaction(
    monkeypatch, fake_create_all, sleeps
):
    engine = _Engine([None])
    monkeypatch.setattr(migrations, "engine", engine)

    await migrations._create_missing_tables()

    assert engine.transactions == 1
    begin, belt, create_all = engine.log
    assert begin == ("begin", 1)
    assert belt[0] == "execute" and "set_config('lock_timeout'" in belt[1]
    assert belt[2] == {"timeout": DEFAULT_LOCK_TIMEOUT}
    assert create_all == ("create_all",)
    assert sleeps == []


@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [
        ("55P03", "canceling statement due to lock timeout"),
        ("57014", "canceling statement due to statement timeout"),
    ],
)
@pytest.mark.asyncio
async def test_lock_contention_is_retried_with_the_runners_backoff(
    monkeypatch, fake_create_all, sleeps, caplog, sqlstate, message
):
    engine = _Engine([_driver_error(sqlstate, message), _driver_error(sqlstate, message), None])
    monkeypatch.setattr(migrations, "engine", engine)

    with caplog.at_level(logging.INFO, logger=migrations.logger.name):
        await migrations._create_missing_tables()

    assert engine.transactions == 3
    # Every attempt is its own transaction, belted again.
    assert [entry[0] for entry in engine.log].count("create_all") == 3
    assert sum("set_config('lock_timeout'" in str(entry) for entry in engine.log) == 3
    assert sleeps == [
        migrations._MIGRATION_RETRY_BASE_DELAY,
        migrations._MIGRATION_RETRY_BASE_DELAY * 2,
    ]
    assert "Database tables ensured" in caplog.text


@pytest.mark.asyncio
async def test_sustained_contention_refuses_to_start_after_the_attempts(
    monkeypatch, fake_create_all, sleeps
):
    attempts = migrations._MIGRATION_MAX_ATTEMPTS
    engine = _Engine(
        [_driver_error("55P03", "canceling statement due to lock timeout") for _ in range(attempts)]
    )
    monkeypatch.setattr(migrations, "engine", engine)

    with pytest.raises(RuntimeError, match="sustained lock contention") as raised:
        await migrations._create_missing_tables()

    assert isinstance(raised.value.__cause__, DBAPIError)
    assert engine.transactions == attempts
    assert len(sleeps) == attempts - 1
    assert max(sleeps) <= migrations._MIGRATION_RETRY_MAX_DELAY


@pytest.mark.asyncio
async def test_the_benign_multi_worker_race_is_still_only_logged(
    monkeypatch, fake_create_all, sleeps, caplog
):
    race = IntegrityError("CREATE TABLE annotations (...)", None, Exception("duplicate key"))
    engine = _Engine([race])
    monkeypatch.setattr(migrations, "engine", engine)

    with caplog.at_level(logging.WARNING, logger=migrations.logger.name):
        await migrations._create_missing_tables()

    assert engine.transactions == 1
    assert sleeps == []
    assert "benign in multi-worker setup" in caplog.text


@pytest.mark.asyncio
async def test_any_other_error_still_fails_at_once(monkeypatch, fake_create_all, sleeps):
    broken = ProgrammingError("CREATE TABLE annotations (...)", None, Exception("syntax error"))
    engine = _Engine([broken])
    monkeypatch.setattr(migrations, "engine", engine)

    with pytest.raises(ProgrammingError):
        await migrations._create_missing_tables()

    assert engine.transactions == 1
    assert sleeps == []
