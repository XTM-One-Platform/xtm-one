"""A short PostgreSQL outage costs latency and 503s, never a flood of 500s.

A restart / failover refused new connections for ~12s on a production platform
and every request that needed a fresh pooled connection failed with a 500 and a
``ConnectionRefusedError`` traceback. The engine now retries such a connect for
``DB_CONNECT_RETRY_SECONDS`` and the HTTP layer answers what is still
unreachable past that with a 503 + ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db_availability
from app.core.db_availability import (
    DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS,
    connect_with_retry,
    database_unavailable_reason,
    install_database_unavailable_handler,
    is_transient_connect_error,
    retrying_connect,
)


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), True),
        (ConnectionResetError(errno.ECONNRESET, "Connection reset"), True),
        (OSError(errno.EHOSTUNREACH, "No route to host"), True),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), True),
        (asyncpg.exceptions.CannotConnectNowError("the database system is starting up"), True),
        (asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already"), True),
        (TimeoutError(), False),
        (OSError(errno.ENOENT, "No such file"), False),
        (asyncpg.exceptions.InvalidPasswordError("password authentication failed"), False),
        (asyncpg.exceptions.InvalidCatalogNameError('database "x" does not exist'), False),
        (ValueError("bad dsn"), False),
    ],
)
def test_only_failures_that_end_on_their_own_are_transient(exc: BaseException, transient: bool) -> None:
    assert is_transient_connect_error(exc) is transient


def test_a_temporary_resolver_failure_is_transient_and_an_unknown_host_is_not() -> None:
    if not hasattr(socket, "EAI_AGAIN"):
        pytest.skip("platform without EAI_AGAIN")
    assert is_transient_connect_error(socket.gaierror(socket.EAI_AGAIN, "Temporary failure"))
    assert not is_transient_connect_error(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _connect_failing(times: int, exc: BaseException, result: object = "connection") -> tuple[Any, list[Any]]:
    calls: list[Any] = []

    async def connect(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        if len(calls) <= times:
            raise exc
        return result

    return connect, calls


async def test_a_refused_connect_is_retried_until_the_database_answers() -> None:
    clock = _FakeClock()
    connect, calls = _connect_failing(3, ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))

    result = await connect_with_retry(
        connect, ("dsn",), {"timeout": 10}, budget_seconds=15, sleep=clock.sleep, clock=clock
    )

    assert result == "connection"
    assert len(calls) == 4
    assert all(call == (("dsn",), {"timeout": 10}) for call in calls)
    assert len(clock.sleeps) == 3
    assert all(0 < delay <= 2.0 for delay in clock.sleeps)


async def test_the_last_error_is_raised_once_the_budget_is_spent() -> None:
    clock = _FakeClock()
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    connect, calls = _connect_failing(10_000, refused)

    with pytest.raises(ConnectionRefusedError) as raised:
        await connect_with_retry(connect, (), {}, budget_seconds=5, sleep=clock.sleep, clock=clock)

    assert raised.value is refused
    assert sum(clock.sleeps) <= 5 + 1e-9
    assert 1 < len(calls) < 50


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        asyncpg.exceptions.InvalidPasswordError("password authentication failed"),
        ValueError("bad dsn"),
    ],
)
async def test_a_failure_that_will_not_go_away_is_raised_at_once(exc: BaseException) -> None:
    clock = _FakeClock()
    connect, calls = _connect_failing(1, exc)

    with pytest.raises(type(exc)):
        await connect_with_retry(connect, (), {}, budget_seconds=15, sleep=clock.sleep, clock=clock)

    assert len(calls) == 1
    assert clock.sleeps == []


async def test_a_zero_budget_disables_the_retry() -> None:
    clock = _FakeClock()
    connect, calls = _connect_failing(1, ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))

    with pytest.raises(ConnectionRefusedError):
        await connect_with_retry(connect, (), {}, budget_seconds=0, sleep=clock.sleep, clock=clock)

    assert len(calls) == 1


def test_the_platform_engine_opens_its_connections_through_the_retry() -> None:
    from app.core.database import ENGINE_CONNECT_ARGS

    assert ENGINE_CONNECT_ARGS["async_creator_fn"] is retrying_connect


async def test_sqlalchemy_calls_the_retrying_connect_for_a_new_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.database import ENGINE_CONNECT_ARGS

    calls: list[dict[str, Any]] = []

    async def refused(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(asyncpg, "connect", refused)
    monkeypatch.setattr(db_availability.settings, "db_connect_retry_seconds", 0.3)

    engine = create_async_engine("postgresql+asyncpg://user:secret@127.0.0.1:1/db", connect_args=ENGINE_CONNECT_ARGS)
    try:
        with pytest.raises(ConnectionRefusedError):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()

    assert len(calls) > 1
    assert calls[0]["timeout"] == ENGINE_CONNECT_ARGS["timeout"]
    assert "async_creator_fn" not in calls[0]


async def test_the_tracing_engine_rides_out_a_refused_connect_like_the_platform_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace flushes have a pool of their own: without the retry a blip drops every trace written meanwhile."""
    from app.core.database import ENGINE_CONNECT_ARGS
    from app.services.tracing import local_sink

    calls: list[dict[str, Any]] = []

    async def refused(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(asyncpg, "connect", refused)
    monkeypatch.setattr(db_availability.settings, "db_connect_retry_seconds", 0.3)
    monkeypatch.setattr(local_sink, "_TRACING_ENGINE", None)
    monkeypatch.setattr(local_sink, "_TRACING_SESSION_FACTORY", None)

    factory = local_sink._build_tracing_session_factory()
    engine = local_sink._TRACING_ENGINE
    try:
        with pytest.raises(ConnectionRefusedError):
            async with factory() as session:
                await session.connection()
    finally:
        await engine.dispose()

    assert len(calls) > 1
    assert calls[0]["timeout"] == ENGINE_CONNECT_ARGS["timeout"]
    assert calls[0]["server_settings"] == {"statement_timeout": "30000"}


class _AdapterError(Exception):
    pass


def _dbapi_error(*, invalidated: bool) -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError("SELECT 1", {}, _AdapterError("boom"), connection_invalidated=invalidated)


def test_what_counts_as_the_database_being_unavailable() -> None:
    assert database_unavailable_reason(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
    assert database_unavailable_reason(_dbapi_error(invalidated=True))
    assert database_unavailable_reason(sa_exc.TimeoutError("QueuePool limit reached"))
    assert database_unavailable_reason(_dbapi_error(invalidated=False)) is None
    assert database_unavailable_reason(sa_exc.IntegrityError("INSERT", {}, _AdapterError("dup"))) is None
    assert database_unavailable_reason(TimeoutError()) is None


def _app_raising(exc: BaseException) -> FastAPI:
    app = FastAPI()
    install_database_unavailable_handler(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return app


async def _get(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/boom")


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        asyncpg.exceptions.CannotConnectNowError("the database system is shutting down"),
        _dbapi_error(invalidated=True),
        sa_exc.TimeoutError("QueuePool limit of size 15 overflow 10 reached"),
    ],
)
async def test_an_unreachable_database_is_a_503_the_client_retries(exc: BaseException) -> None:
    response = await _get(_app_raising(exc))

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS)
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi_error(invalidated=False),
        sa_exc.IntegrityError("INSERT", {}, _AdapterError("duplicate key")),
        OSError(errno.ENOENT, "No such file"),
    ],
)
async def test_any_other_error_is_still_a_500(exc: BaseException) -> None:
    response = await _get(_app_raising(exc))

    assert response.status_code == 500


def test_the_platform_app_answers_an_unreachable_database_with_a_503() -> None:
    from app.main import application

    for exc_class in db_availability.DATABASE_UNAVAILABLE_EXCEPTIONS:
        assert application.exception_handlers.get(exc_class) is db_availability.database_unavailable_handler


# --- The startup's database phase rides out a failover (#10368) ---


class _PostgresError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _raised_from(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


def _from_the_engine_connect(exc: BaseException) -> BaseException:
    exc.add_note(db_availability.DATABASE_CONNECT_NOTE)
    return exc


def _raised_while_handling(outer: BaseException, handled: BaseException) -> BaseException:
    outer.__context__ = handled
    return outer


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (_dbapi_error(invalidated=True), True),
        (asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"), True),
        (asyncpg.exceptions.AdminShutdownError("terminating connection"), True),
        (sa_exc.OperationalError("SELECT 1", {}, _PostgresError("57P01")), True),
        (_PostgresError("25006"), True),
        (sa_exc.TimeoutError("QueuePool limit reached"), True),
        (_from_the_engine_connect(TimeoutError()), True),
        (_from_the_engine_connect(ConnectionRefusedError(errno.ECONNREFUSED, "refused")), True),
        # The migrations re-raise every failure as a RuntimeError.
        (_raised_from(RuntimeError("Alembic migration failed"), _dbapi_error(invalidated=True)), True),
        # Not from the database: an object store, an HTTP client, an asyncio.wait_for.
        (TimeoutError(), False),
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), False),
        (_raised_from(RuntimeError("upload failed"), ConnectionRefusedError(errno.ECONNREFUSED, "refused")), False),
        # Raised while handling a lost connection, not caused by it.
        (_raised_while_handling(ValueError("bad seed row"), _dbapi_error(invalidated=True)), False),
        (_from_the_engine_connect(asyncpg.exceptions.InvalidPasswordError("bad password")), False),
        (_PostgresError("08P01"), False),
        (RuntimeError("unknown Alembic revision"), False),
        (_dbapi_error(invalidated=False), False),
        (_PostgresError("57014"), False),
        (sa_exc.IntegrityError("INSERT", {}, _PostgresError("23505")), False),
    ],
)
def test_what_the_startup_waits_out(exc: BaseException, transient: bool) -> None:
    assert db_availability.is_transient_database_error(exc) is transient


def test_a_context_suppressed_with_from_none_is_not_the_cause() -> None:
    try:
        try:
            raise _dbapi_error(invalidated=True)
        except sa_exc.DBAPIError:
            raise RuntimeError("unknown Alembic revision") from None
    except RuntimeError as exc:
        assert db_availability.is_transient_database_error(exc) is False


def test_a_cyclic_cause_chain_ends_the_walk() -> None:
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first
    assert db_availability.is_transient_database_error(first) is False


def test_the_sqlstates_of_a_wrapped_driver_error_are_read() -> None:
    wrapped = sa_exc.ProgrammingError("SELECT", {}, _PostgresError("42P01"))
    assert db_availability.sqlstates_in_chain(_raised_from(RuntimeError("x"), wrapped)) == {"42P01"}


@pytest.mark.parametrize(
    ("exc", "label"),
    [
        # A demoted primary through SQLAlchemy: the wrapper is a bare DBAPIError.
        (sa_exc.DBAPIError("INSERT", {}, _PostgresError("25006")), "25006"),
        (_raised_from(RuntimeError("Alembic migration failed"), asyncpg.exceptions.AdminShutdownError("x")), "57P01"),
        (
            _from_the_engine_connect(
                ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed ('10.0.0.1', 5432)")
            ),
            "ConnectionRefusedError",
        ),
        (
            _raised_from(_dbapi_error(invalidated=True), asyncpg.exceptions.InterfaceError("connection is closed")),
            "InterfaceError",
        ),
    ],
)
def test_the_outage_is_named_by_its_cause_never_its_message(exc: BaseException, label: str) -> None:
    assert db_availability.outage_label(exc) == label


async def test_a_connect_that_gives_up_says_it_was_the_database() -> None:
    connect, _ = _connect_failing(10, ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    with pytest.raises(ConnectionRefusedError) as raised:
        await connect_with_retry(connect, (), {}, budget_seconds=0)

    assert db_availability.DATABASE_CONNECT_NOTE in raised.value.__notes__


class _Engine:
    def __init__(self) -> None:
        self.disposals = 0

    async def dispose(self) -> None:
        self.disposals += 1


@pytest.fixture
def app_engine(monkeypatch) -> _Engine:
    engine = _Engine()
    monkeypatch.setattr("app.core.database.engine", engine)
    return engine


async def test_the_startup_database_phase_retries_a_lost_connection_until_it_succeeds(app_engine) -> None:
    outcomes: list[BaseException | str] = [
        _dbapi_error(invalidated=True),
        _from_the_engine_connect(TimeoutError()),
        "adopted",
    ]
    sleeps: list[float] = []
    statuses: list[str | None] = []

    async def run() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def on_status(exc: BaseException | None) -> None:
        statuses.append(None if exc is None else type(exc).__name__)

    assert (
        await db_availability.retry_while_database_unavailable("step", run, on_status=on_status, sleep=sleep)
        == "adopted"
    )
    assert statuses == ["DBAPIError", "TimeoutError", None]
    assert 0.5 <= sleeps[0] <= 1.0
    assert 1.0 <= sleeps[1] <= 2.0
    # A pooled connection to a now read-only primary passes the pre-ping: never reused.
    assert app_engine.disposals == 2


async def test_the_startup_back_off_is_capped_and_never_gives_up(app_engine) -> None:
    failures = 12
    sleeps: list[float] = []

    async def run() -> str:
        if len(sleeps) < failures:
            raise _dbapi_error(invalidated=True)
        return "done"

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert await db_availability.retry_while_database_unavailable("step", run, sleep=sleep) == "done"
    assert len(sleeps) == failures
    assert 15.0 <= max(sleeps) <= 30.0


async def test_a_failure_the_database_coming_back_will_not_fix_is_raised_at_once(app_engine) -> None:
    statuses: list[BaseException | None] = []

    async def run() -> None:
        raise RuntimeError("unknown Alembic revision")

    async def sleep(seconds: float) -> None:
        raise AssertionError("must not retry")

    with pytest.raises(RuntimeError, match="unknown Alembic revision"):
        await db_availability.retry_while_database_unavailable("step", run, on_status=statuses.append, sleep=sleep)
    assert statuses == []


async def test_the_outage_is_cleared_whatever_ends_the_retrying(app_engine) -> None:
    """A permanent error after a retry, or a cancelled wait, never leaves the outage named."""
    outcomes: list[BaseException] = [_dbapi_error(invalidated=True), RuntimeError("unknown Alembic revision")]
    statuses: list[str | None] = []

    async def run() -> None:
        raise outcomes.pop(0)

    async def sleep(seconds: float) -> None:
        return None

    def on_status(exc: BaseException | None) -> None:
        statuses.append(None if exc is None else type(exc).__name__)

    with pytest.raises(RuntimeError):
        await db_availability.retry_while_database_unavailable("step", run, on_status=on_status, sleep=sleep)
    assert statuses == ["DBAPIError", None]

    statuses.clear()

    async def lose_the_connection() -> None:
        raise _dbapi_error(invalidated=True)

    async def cancelled(seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await db_availability.retry_while_database_unavailable(
            "step", lose_the_connection, on_status=on_status, sleep=cancelled
        )
    assert statuses == ["DBAPIError", None]
