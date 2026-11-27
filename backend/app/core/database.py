# SPDX-License-Identifier: Apache-2.0

"""Async SQLAlchemy engine and session factory.

Connection pool is sized via environment variables (see core/config.py).
Default: 10 persistent + 10 overflow = 20 max connections per process.
For production with multiple uvicorn workers, ensure
  (pool_size + max_overflow) * (web_workers + saq_workers)
    < max_connections in postgresql.conf  (default pg max_connections = 100).
"""

import asyncio
import contextlib
import functools
import json
import logging
import re
import time
import traceback
from collections.abc import AsyncGenerator, Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.cancellation import run_cleanup_to_completion
from app.core.config import settings
from app.core.db_availability import retrying_connect
from app.core.json_sql import REFUSED_ESCAPE_PATTERN
from app.core.redaction import current_known_secrets, redact_known_secrets
from app.core.utils import (
    null_non_finite_numbers,
    sanitize_json_strings,
    storable_text,
    strip_null_bytes,
)

_pool_logger = logging.getLogger("app.pool")


def json_serializer(value: object) -> str:
    """Serialize ORM JSON column values with NUL bytes stripped and lone surrogates replaced.

    PostgreSQL ``json`` columns store the serialized text verbatim, so a
    ``\\u0000`` escape arriving in an external payload (webhook bodies,
    integration events) persists silently — and poisons any later read that
    parses the column as ``jsonb``, which cannot represent NUL and raises
    ``UntranslatableCharacterError``, e.g. the agent-builder ``list_runs``
    cross-run search. A lone UTF-16 surrogate (a request body's
    ``"\\ud800"``) is written as the same kind of escape, which every such
    read refuses too, and one agent's bindings holding one took the provider
    webhook route down for every provider: it becomes U+FFFD, a valid
    pair is kept (:func:`~app.core.utils.sanitize_json_strings`). Sanitizing
    in the engine's serializer covers every ORM JSON write in one place
    instead of chasing individual entry points (webhook receiver, integration
    events, chat handovers, run retries copying an old payload). The secret
    variable values the run or turn in progress resolved are stored as their
    marker for the same reason (``core/redaction.py``).

    ``NaN``/``Infinity`` ride in through the same door and are refused by the
    same columns: JSON cannot spell either, so PostgreSQL rejects the
    write (``invalid input syntax for type json``) and the whole transaction
    with it, yet ``json.loads`` accepts the bare tokens and ``json.dumps``
    writes them back. Nulled here for the same reason: one place, not every
    caller.
    """
    return json.dumps(
        null_non_finite_numbers(sanitize_json_strings(redact_known_secrets(value)))
    )


#: A ``\\u0000`` or surrogate escape in stored JSON text: the only way a
#: decoded value can hold what :func:`json_serializer` no longer stores.
_UNSTORED_ESCAPE_RE = re.compile(REFUSED_ESCAPE_PATTERN)


def json_deserializer(text: str) -> object:
    """Decode a JSON column value as :func:`json_serializer` would store it.

    A row written before the serializer replaced lone surrogates may still hold
    one, and it decodes to a string no JSON response, HTTP client or LLM SDK
    can encode: one company-managed agent with such bindings failed every agent
    list that serialized it. The stored text is searched first, so a clean
    value costs one scan and comes back as ``json.loads`` returns it.
    """
    value = json.loads(text)
    return sanitize_json_strings(value) if _UNSTORED_ESCAPE_RE.search(text) else value


def _map_keeping_shape(values: list | tuple, clean: Callable[[Any], Any]) -> list | tuple:
    """Apply *clean* to each item; the same object back when nothing changed."""
    cleaned: list | None = None
    for idx, value in enumerate(values):
        new_value = clean(value)
        if cleaned is None:
            if new_value is value:
                continue
            cleaned = list(values[:idx])
        cleaned.append(new_value)
    if cleaned is None:
        return values
    return tuple(cleaned) if isinstance(values, tuple) else cleaned


def _storable_nested(value: Any) -> Any:
    # Inside an ARRAY bind or a named row: every string through storable_text
    # itself - stripping its NULs first could join the halves one separated -
    # and containers walked as strip_null_bytes walks them (a tuple becomes a
    # list, a dict loses its NULs).
    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, list):
        return _map_keeping_shape(value, _storable_nested)
    if isinstance(value, tuple):
        return [_storable_nested(item) for item in value]
    return strip_null_bytes(value) if isinstance(value, dict) else value


def _strip_null_bytes_from_value(value: Any) -> Any:
    # A str is made storable, the items of a list (an ARRAY bind) too; a dict
    # (a JSON-shaped bind) loses its NULs; bytes, a tuple and every other type
    # pass through untouched.
    if isinstance(value, str | list | dict):
        return _storable_nested(value)
    return value


def _strip_null_bytes_from_row(row: Any) -> Any:
    if isinstance(row, dict):
        cleaned: dict | None = None
        for idx, (key, value) in enumerate(row.items()):
            new_key = key.replace("\x00", "") if isinstance(key, str) and "\x00" in key else key
            new_value = _storable_nested(value)
            if cleaned is None:
                if new_key is key and new_value is value:
                    continue
                cleaned = dict(list(row.items())[:idx])
            cleaned[new_key] = new_value
        return row if cleaned is None else cleaned
    if isinstance(row, list | tuple):
        return _map_keeping_shape(row, _strip_null_bytes_from_value)
    return row


def strip_null_bytes_from_parameters(parameters: Any, executemany: bool) -> Any:
    """Make every string bound to a statement one PostgreSQL text can hold.

    PostgreSQL refuses a NUL byte in any text value
    (``CharacterNotInRepertoireError``), so a request carrying one in a plain
    ``String`` / ``Text`` column - an agent name, a prompt, a conversation
    title - failed with a 500; so did a UTF-16 surrogate, which the driver
    cannot encode as UTF-8. Both are repaired at the driver boundary
    (:func:`~app.core.utils.storable_text`: the NUL stripped, a surrogate pair
    joined into its character, a lone surrogate U+FFFD), the counterpart of
    :func:`json_serializer` for JSON columns: one place covering
    every write path (ORM flush, Core statements, ``text()``), and no such
    string could ever have been accepted by PostgreSQL. A JSON value is bound
    as the serializer's ASCII text, so this never changes one. Copy-on-write,
    and the row shape (tuple, list, dict) is kept.
    """
    if executemany and isinstance(parameters, list | tuple):
        return _map_keeping_shape(parameters, _strip_null_bytes_from_row)
    return _strip_null_bytes_from_row(parameters)


def _holds_content(type_: Any) -> bool:
    """A column type that holds text a run writes (a message, a summary, a document), not a status or a key.

    Only these have the known secret values of the run or turn redacted:
    a short ``String(n)`` (a status, a slug, a name) is structure, and
    a secret that happens to equal one of its values must not rewrite it.
    """
    if isinstance(type_, sqltypes.TypeDecorator):
        type_ = type_.impl_instance
    if isinstance(type_, sqltypes.JSON | sqltypes.Text):
        return True
    return isinstance(type_, sqltypes.String) and type_.length is None


_BATCH_SUFFIX_RE = re.compile(r"__\d+$")


def _content_bind_names(context: Any) -> frozenset[str]:
    """The bound parameters of a write that a content column takes: VALUES / SET only, never a WHERE value."""
    compiled = getattr(context, "compiled", None)
    binds = getattr(compiled, "binds", None)
    if not binds:
        return frozenset()
    return frozenset(
        name for name, bind in binds.items() if getattr(bind, "_is_crud", False) and _holds_content(bind.type)
    )


def _redact_content_parameters(parameters: Any, executemany: bool, context: Any) -> Any:
    names = _content_bind_names(context)
    if not names:
        return parameters
    positions = getattr(getattr(context, "compiled", None), "positiontup", None) or []

    def _row(row: Any) -> Any:
        if isinstance(row, dict):
            # A batched INSERT names a row's parameter ``content__1`` (SQLAlchemy's insertmanyvalues).
            return {
                key: redact_known_secrets(value) if _BATCH_SUFFIX_RE.sub("", key) in names else value
                for key, value in row.items()
            }
        # A batched INSERT (insertmanyvalues, ``... RETURNING``) sends its rows as one flat
        # positional sequence, each row's parameters in ``positiontup`` order.
        if isinstance(row, list | tuple) and positions and len(row) % len(positions) == 0:
            width = len(positions)
            cleaned = [redact_known_secrets(v) if positions[i % width] in names else v for i, v in enumerate(row)]
            return tuple(cleaned) if isinstance(row, tuple) else cleaned
        return row

    if executemany and isinstance(parameters, list | tuple) and parameters and isinstance(parameters[0], dict | list | tuple):
        return type(parameters)(_row(row) for row in parameters)
    return _row(parameters)


# On the Engine CLASS, not on one engine: every engine of the process (this
# one, the tracing engine, the startup checks) talks to the same PostgreSQL.
@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _strip_null_bytes_before_execute(conn, cursor, statement, parameters, context, executemany):
    parameters = strip_null_bytes_from_parameters(parameters, executemany)
    # A Core write of the run or turn in progress (the ORM's are redacted at
    # flush): a secret variable value it resolved is stored as its marker in the
    # content columns it writes. A text() statement is written as given.
    if current_known_secrets():
        parameters = _redact_content_parameters(parameters, executemany, context)
    return statement, parameters


def _is_json_type(type_: Any) -> bool:
    """A JSON column type, a ``TypeDecorator`` over one included (``SpecJSON``)."""
    if isinstance(type_, sqltypes.TypeDecorator):
        type_ = type_.impl_instance
    return isinstance(type_, sqltypes.JSON)


@functools.cache
def _json_attribute_keys(mapper: Any) -> frozenset[str]:
    return frozenset(attr.key for attr in mapper.column_attrs if _is_json_type(attr.columns[0].type))


@functools.cache
def _content_attribute_keys(mapper: Any) -> frozenset[str]:
    return frozenset(attr.key for attr in mapper.column_attrs if _holds_content(attr.columns[0].type))


# The engine guard decides what is stored; this keeps the flushed objects in
# step with it, so a response built from one after the write says what the
# database holds rather than echoing the NUL or a surrogate - or a
# lone surrogate of a JSON value being written, which no JSON response can
# even encode - or a secret variable value the run resolved.
@event.listens_for(Session, "before_flush")
def _strip_null_bytes_before_flush(session, flush_context, instances):
    redacting = bool(current_known_secrets())
    for obj in (*session.new, *session.dirty):
        state = sa_inspect(obj)
        json_keys = _json_attribute_keys(state.mapper)
        content_keys = _content_attribute_keys(state.mapper)
        for attr in state.mapper.column_attrs:
            value = state.dict.get(attr.key)
            if isinstance(value, str):
                stored = storable_text(value)
                # A content column this flush writes (an UPDATE carries the changed
                # columns only); a status or a key is never rewritten.
                if redacting and attr.key in content_keys and state.attrs[attr.key].history.has_changes():
                    stored = redact_known_secrets(stored)
                if stored is not value:
                    value = stored
                    setattr(obj, attr.key, value)
            if (
                attr.key in json_keys
                and value is not None
                and state.attrs[attr.key].history.has_changes()
            ):
                clean = sanitize_json_strings(redact_known_secrets(value))
                if clean is not value:
                    setattr(obj, attr.key, clean)


ENGINE_CONNECT_ARGS: dict[str, Any] = {
    # Bound connection establishment so a bad connect fails fast (the
    # reconciler then retries / self-recovers) instead of stalling a whole
    # cycle for asyncpg's 60s default.
    "timeout": settings.db_connect_timeout,
    "server_settings": {"statement_timeout": "30000"},
    # A connect PostgreSQL refuses while it restarts or fails over is retried
    # for DB_CONNECT_RETRY_SECONDS instead of failing the request with a 500.
    "async_creator_fn": retrying_connect,
}

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    json_serializer=json_serializer,
    json_deserializer=json_deserializer,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=settings.db_pool_pre_ping,
    connect_args=ENGINE_CONNECT_ARGS,
)

_pool_capacity = settings.db_pool_size + settings.db_max_overflow

#: The readiness probe's own engine (#2555): one connection, never the request pool. Under a burst
#: that held every request connection, ``/api/health/ready`` waited on that pool, timed out and
#: answered 503 on every pod at once, so the ingress dropped a platform whose database was up.
_health_engine: Any = None


def health_engine() -> Any:
    """The engine ``/api/health/ready`` reads the database with: a pool of one, created on first use."""
    global _health_engine
    if _health_engine is None:
        _health_engine = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
            connect_args=ENGINE_CONNECT_ARGS,
        )
    return _health_engine


# ── Pool-holder diagnostics ─────────────────────────────────────────
# The bare "Pool near exhaustion: N/N" warning proved useless for root-causing
# exhaustion: it fires on EVERY checkout at >= capacity-2 (flooding the logs)
# and says nothing about WHO holds the other 39 connections. We now:
#   * record, per checked-out connection, the owning asyncio task name, the
#     checkout timestamp, and a short code location (the first app frame of
#     the checkout stack) — cheap enough for the hot path;
#   * debounce the warning to once per _WARN_INTERVAL_S per process;
#   * include the oldest holders (age + task + code location) in the warning,
#     so a production log line directly names the code paths pinning the pool.
_holders: dict[int, dict] = {}
_WARN_INTERVAL_S = 15.0
_TOP_HOLDERS_IN_WARNING = 8


def _is_app_frame(filename: str) -> bool:
    fn = filename.replace("\\", "/")
    return "/app/" in fn and "core/database.py" not in fn


def _shorten(filename: str, lineno: int) -> str:
    fn = filename.replace("\\", "/")
    return f"{fn.rsplit('/app/', 1)[-1]}:{lineno}"


def _first_app_frame() -> str:
    """Best-effort ``module:line`` of the innermost app frame at checkout.

    The sync ``checkout`` event runs inside SQLAlchemy's greenlet, whose own
    stack only contains SQLAlchemy internals — the async caller's coroutine
    frames live in the PARENT greenlet, suspended at the ``await_only``
    switch point. Walk that suspended frame chain first (it climbs through
    the resumed coroutine frames of the owning task), then fall back to the
    current stack for non-greenlet callers. Diagnostics only — never raises.
    """
    # Diagnostics only — a failure here must never break the checkout, so
    # both probes swallow exceptions deliberately (no logging: this runs on
    # the hot path for every checkout).
    with contextlib.suppress(Exception):
        import greenlet as _greenlet

        parent = getattr(_greenlet.getcurrent(), "parent", None)
        frame = getattr(parent, "gr_frame", None)
        depth = 0
        while frame is not None and depth < 80:
            if _is_app_frame(frame.f_code.co_filename):
                return _shorten(frame.f_code.co_filename, frame.f_lineno)
            frame = frame.f_back
            depth += 1
    with contextlib.suppress(Exception):
        for fr in reversed(traceback.extract_stack(limit=60)):
            if _is_app_frame(fr.filename):
                return _shorten(fr.filename, fr.lineno or 0)
    return "unknown"


def _current_task_name() -> str:
    try:
        task = asyncio.current_task()
        return task.get_name() if task is not None else "no-task"
    except Exception:  # pragma: no cover
        return "no-loop"


def _log_context() -> str:
    """Snapshot the structlog contextvars bound by the owning request/loop.

    SQLAlchemy's greenlet runs in the caller's contextvars context, so the
    ambient ``source`` / ``agent`` / conversation bindings identify the
    owning operation far better than a generic task name.
    """
    try:
        import structlog.contextvars as _scv

        ctx = _scv.get_contextvars()
        parts = [
            f"{k}={ctx[k]}"
            for k in ("source", "agent", "conversation_id", "assignment_id")
            if ctx.get(k)
        ]
        return ",".join(parts)
    except Exception:  # pragma: no cover
        return ""


def _format_oldest_holders(now: float) -> str:
    oldest = sorted(_holders.values(), key=lambda h: h["since"])[
        :_TOP_HOLDERS_IN_WARNING
    ]
    rendered = []
    for h in oldest:
        ctx = f" [{h['ctx']}]" if h.get("ctx") else ""
        rendered.append(
            f"{h['task']} @{h['where']}{ctx} held {now - h['since']:.1f}s"
        )
    return "; ".join(rendered)


@event.listens_for(engine.sync_engine, "checkout")
def _on_checkout(dbapi_conn, connection_record, connection_proxy):
    now = time.monotonic()
    _holders[id(connection_record)] = {
        "task": _current_task_name(),
        "where": _first_app_frame(),
        "ctx": _log_context(),
        "since": now,
    }
    pool = connection_proxy._pool
    checked_out = pool.checkedout()
    if checked_out >= _pool_capacity - 2:
        # Debounce state lives on the listener itself rather than in a
        # mutable module global.
        last_warn = getattr(_on_checkout, "_last_warn_monotonic", 0.0)
        if now - last_warn < _WARN_INTERVAL_S:
            return
        _on_checkout._last_warn_monotonic = now
        _pool_logger.warning(
            "Pool near exhaustion: %d/%d connections checked out "
            "(pool_size=%d, overflow=%d). Oldest holders: %s",
            checked_out,
            _pool_capacity,
            settings.db_pool_size,
            settings.db_max_overflow,
            _format_oldest_holders(now) or "none tracked",
        )


@event.listens_for(engine.sync_engine, "checkin")
def _on_checkin(dbapi_conn, connection_record):
    _holders.pop(id(connection_record), None)


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

#: The queued signal intake's own engine (#2584): an OpenCTI catch-up records its signals on
#: this budget (``CTEM_SIGNAL_INTAKE_DB_POOL_SIZE``), never on the web pool the UI waits on.
_intake_sessionmaker: Any = None


def intake_session() -> AsyncSession:
    """A session on the intake engine, created on first use: no overflow, the drain waits instead."""
    global _intake_sessionmaker
    if _intake_sessionmaker is None:
        intake_engine = create_async_engine(
            settings.database_url,
            json_serializer=json_serializer,
            json_deserializer=json_deserializer,
            pool_size=max(1, settings.ctem_signal_intake_db_pool_size),
            max_overflow=0,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=settings.db_pool_pre_ping,
            connect_args=ENGINE_CONNECT_ARGS,
        )
        _intake_sessionmaker = async_sessionmaker(intake_engine, class_=AsyncSession, expire_on_commit=False)
    session: AsyncSession = _intake_sessionmaker()
    return session


class StatementScopedSession:
    """*session*, handing its connection back to the pool after every statement.

    For read-only work that waits on the network between its queries: a
    prompt guard reads its rules and providers between embedding and model
    calls that can take seconds each, and an ordinary session keeps its
    connection checked out from the first query to the last, so a batch of
    evaluations would hold the pool through every one of those calls. Every
    other attribute is the wrapped session's; the rows it loads stay usable
    because ``async_session`` does not expire them on commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def _released(self, call: Any) -> Any:
        try:
            result = await call
        except BaseException:
            await self._session.rollback()
            raise
        await self._session.commit()
        return result

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await self._released(self._session.execute(*args, **kwargs))

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        return await self._released(self._session.scalar(*args, **kwargs))

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        return await self._released(self._session.scalars(*args, **kwargs))

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._released(self._session.get(*args, **kwargs))


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


async def release_db_connection_before_loop(db: AsyncSession) -> None:
    """Release any checked-out DB connection before a long agentic loop.

    The agentic loop (LLM + tool calls) can run for minutes. A session with
    an open transaction pins a pool connection for that whole time, so ~15
    concurrent chats/assignments exhaust the default 10+10 pool. Committing
    here returns the connection to the pool; SQLAlchemy transparently
    re-acquires one on the session's next use (post-loop persistence keeps
    working unchanged).

    Commits any open transaction, so callers should invoke this only after
    completing all DB work that must happen before the long-running loop.

    If the commit itself fails (e.g. integrity error, broken connection),
    a best-effort ``rollback()`` is issued before returning. SQLAlchemy
    sessions enter a failed-transaction state after a commit error and
    will refuse subsequent operations until the transaction is closed —
    rolling back here keeps the session usable for downstream code in the
    same run and lets the underlying connection be returned to the pool
    instead of staying pinned to a dead transaction.
    """
    try:
        if db.in_transaction():
            await db.commit()
    except asyncio.CancelledError:
        # Cancellation must keep propagating (request teardown / shutdown).
        # ``CancelledError`` is a ``BaseException``, so the ``except
        # Exception`` below would not catch it anyway; this clause makes the
        # intent explicit and survives future edits (mirrors ``get_db``).
        raise
    except Exception:
        _pool_logger.warning(
            "Failed to release DB connection before loop — continuing with held connection",
            exc_info=True,
        )
        # Best-effort rollback so the session is left in a usable state.
        # Without this, the next ``db.execute()`` or ``db.commit()`` in
        # the caller would raise "This transaction is closed/in a failed
        # state" and prevent the engine from releasing the connection back
        # to the pool — which is precisely the pool-starvation symptom
        # this helper is meant to mitigate.
        try:
            await db.rollback()
        except asyncio.CancelledError:
            raise
        except Exception:
            _pool_logger.debug(
                "Best-effort rollback after failed pre-loop commit also failed",
                exc_info=True,
            )


async def get_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency – yields an async DB session.

    Session teardown runs through
    :func:`~app.core.cancellation.run_cleanup_to_completion`, which closes the
    session on a shielded child task. Without it, a cancellation propagating
    into the request task — e.g. when MCP server cleanup tears down its anyio
    ``TaskGroup`` — interrupts the asyncpg ROLLBACK that SQLAlchemy issues
    inside ``session.close()`` on the first ``await``, surfacing as an
    unhandled ``CancelledError`` ASGI exception at uvicorn and returning a
    connection to the pool in an inconsistent state.

    The session is created directly rather than via
    ``async with async_session() as session`` on purpose: the context
    manager's ``__aexit__`` would call ``session.close()`` a second time,
    re-introducing an unguarded close on the cancellation path.

    This commit runs on dependency teardown, which FastAPI executes *after*
    the response is sent. A handler returning an id the caller will use on its
    next request must therefore commit itself, or that request can arrive
    before the row lands and read it as missing. Building the response after
    that commit is safe only because ``async_session`` sets
    ``expire_on_commit=False``; with it on, the attributes read back would
    emit lazy-load SELECTs on a finished transaction. Every route is held to
    this by ``tests/test_write_routes_commit_before_response.py``.
    """
    session = async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        # Always finish closing the session (and its ROLLBACK) even if the
        # request task is being cancelled, so the pooled connection is
        # returned cleanly.
        try:
            await run_cleanup_to_completion(session.close())
        except asyncio.CancelledError:
            # The cleanup task itself was cancelled (e.g. event-loop
            # shutdown). ``run_cleanup_to_completion`` surfaces that case on
            # purpose — never swallow it, so the cancellation still reaches
            # the caller. (``CancelledError`` is a ``BaseException``, so the
            # ``except Exception`` below would not catch it anyway; this clause
            # makes the intent explicit and survives future edits.)
            raise
        except Exception:
            # A non-cancellation close failure is logged, not propagated, so
            # it never masks the exception already unwinding through this
            # finally.
            _pool_logger.debug(
                "Error closing DB session during cleanup", exc_info=True
            )
