"""The Redis the unit suite runs against: none.

CI's unit job starts no Redis, so every command a unit test sends is refused.
A developer's machine usually runs one, and a test that reaches it there writes
keys that outlive the run and passes or fails with the test order (the shared
client's pooled connection belongs to the event loop of the test that opened
it). ``tests/conftest.py`` therefore runs every test not marked
``integration`` inside :func:`refusing`.

Clients stay real ``redis.asyncio`` clients: pipelines, pub/sub, locks and
scripts behave as they do against a host that refuses the connection, and
every command raises the ``redis.exceptions.ConnectionError`` CI's unit job
raises. Nothing touches the network.

Nothing here goes through a test's ``monkeypatch``: a test that undoes its
own, or patches the same names, can neither lift the refusal nor carry it
past its own end, whatever order pytest tears fixtures down in.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
from collections.abc import Iterator

import redis.asyncio as redis_asyncio
import redis.connection as redis_sync_connection
from redis.asyncio.connection import Connection, UnixDomainSocketConnection
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

UNREACHABLE_HOST = "no-redis-in-the-unit-suite.invalid"
REFUSAL = "no Redis in the unit suite"

# redis-py's socket connection classes, asyncio and sync: every other one
# (SSL, Sentinel managed) reaches ``Connection._connect``.
_SOCKET_CLASSES = (
    Connection,
    UnixDomainSocketConnection,
    redis_sync_connection.Connection,
    redis_sync_connection.UnixDomainSocketConnection,
)
_refusing = False


async def _refused(self) -> None:
    raise ConnectionRefusedError(errno.ECONNREFUSED, REFUSAL)


class RefusedConnection(Connection):
    """A connection whose socket is always refused."""

    _connect = _refused


def unreachable_redis() -> tuple[redis_asyncio.Redis, redis_asyncio.ConnectionPool]:
    """A fresh shared client and its pool, refusing each connection once
    (the real client tries a refused connection a second time)."""
    pool = redis_asyncio.ConnectionPool(
        connection_class=RefusedConnection,
        host=UNREACHABLE_HOST,
        decode_responses=True,
        retry=Retry(NoBackoff(), 0),
    )
    return redis_asyncio.Redis(connection_pool=pool), pool


def _switchable(original):
    if inspect.iscoroutinefunction(original):

        async def _connect(self):
            if _refusing:
                await _refused(self)
            return await original(self)

    else:

        def _connect(self):
            if _refusing:
                raise ConnectionRefusedError(errno.ECONNREFUSED, REFUSAL)
            return original(self)

    _connect.refuses_in_the_unit_suite = True
    return _connect


@contextlib.contextmanager
def switchable_sockets() -> Iterator[None]:
    """Let :func:`refusing` refuse every socket connection, for the session.

    A client built anywhere but ``app.core.redis`` (the SAQ queue's, a
    Sentinel's, an ad-hoc ``from_url``, a sync ``redis.Redis``) connects
    through these classes; they connect as usual while no test is refusing.
    """
    originals = {cls: cls.__dict__["_connect"] for cls in _SOCKET_CLASSES}
    for cls, original in originals.items():
        cls._connect = _switchable(original)
    try:
        yield
    finally:
        for cls, original in originals.items():
            cls._connect = original


def real_redis() -> None:
    """Rebuild the real shared client if a refusing one is still in place.

    A test that patched ``_shared_client`` or ``_pool`` itself puts back,
    after its end, whatever it found - a refusing pair.

    A real pool replaced here is dropped, not disconnected: its connections
    belong to the closed event loop of the test that opened them, which no
    later loop can close (``disconnect()`` raises "Event loop is closed").
    That is the state the real shared pool's stale connections are already
    left in between integration tests.
    """
    from app.core import redis as core_redis

    # ``redis_pubsub_client()`` builds on ``_pool``, not on the shared
    # client's pool: either may be the one put back.
    shared_pool = core_redis._shared_client.connection_pool
    pool = core_redis._pool
    if (
        shared_pool.connection_class is RefusedConnection
        or (pool is not None and pool.connection_class is RefusedConnection)
        or (pool is not None and shared_pool is not pool)
    ):
        core_redis._build_client()


def refusing_now() -> bool:
    """Whether every socket connection is refused at this moment."""
    return _refusing


@contextlib.contextmanager
def refusing() -> Iterator[None]:
    """Refuse every Redis connection inside the block.

    ``redis_client()`` and ``redis_pubsub_client()`` read the shared client
    and pool of ``app.core.redis`` on every call, so swapping both there
    covers every module, whichever way it imported them - with a fresh pair,
    so nothing is shared between tests. Every other client is refused through
    :func:`switchable_sockets`.

    Nestable: an inner block rebuilds and restores a fresh real pair; only
    the outermost block puts back the pair that was there before the test.
    """
    global _refusing
    from app.core import redis as core_redis

    real_redis()
    real = core_redis._shared_client, core_redis._pool
    was_refusing = _refusing
    core_redis._shared_client, core_redis._pool = unreachable_redis()
    _refusing = True
    try:
        yield
    finally:
        _refusing = was_refusing
        core_redis._shared_client, core_redis._pool = real
