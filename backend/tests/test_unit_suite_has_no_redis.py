"""The unit suite meets no Redis, as CI's unit job does.

``tests/conftest.py::_no_redis_in_the_unit_suite`` runs every test not marked
``integration`` inside ``tests/redis_unreachable.refusing``. These tests pin
what a unit test then meets on every path to Redis, and that an integration
test keeps the real Redis.
"""

from __future__ import annotations

import pytest
import redis.asyncio as redis_asyncio
import redis.exceptions as redis_exceptions
from redis.asyncio.connection import Connection
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import MasterNotFoundError, Sentinel
from redis.backoff import NoBackoff

from tests import redis_unreachable
from tests.redis_unreachable import REFUSAL, UNREACHABLE_HOST, RefusedConnection


@pytest.fixture
def connects(monkeypatch):
    """Every socket connection attempted, as the host it was aimed at."""
    seen: list[str] = []
    refuse = RefusedConnection._connect

    async def _spy(self):
        seen.append(self.host)
        await refuse(self)

    monkeypatch.setattr(RefusedConnection, "_connect", _spy)
    return seen


async def test_the_shared_client_refuses_every_command_once(connects):
    from app.core.redis import redis_client

    client = await redis_client()

    with pytest.raises(redis_exceptions.ConnectionError, match=REFUSAL):
        await client.set("a2a:dedup:partner-1:m-2", "task", nx=True, ex=86400)

    assert connects == [UNREACHABLE_HOST]


async def test_a_module_that_imported_redis_client_meets_the_same_refusal(connects):
    # Imported at module level, as most app modules do: the name is bound to
    # the function, which reads the swapped client on every call.
    from app.services import token_store

    client = await token_store.redis_client()

    with pytest.raises(redis_exceptions.ConnectionError):
        await client.get("any")

    assert connects == [UNREACHABLE_HOST]


@pytest.mark.parametrize(
    "use",
    [
        pytest.param(lambda c: c.pipeline().set("a", 1).incr("b").execute(), id="pipeline"),
        pytest.param(lambda c: c.eval("return 1", 0), id="script"),
        pytest.param(lambda c: c.lock("l", timeout=5).acquire(blocking=False), id="lock"),
        pytest.param(lambda c: c.pubsub().subscribe("channel"), id="pubsub"),
    ],
)
async def test_every_other_way_to_reach_the_shared_client_is_refused(connects, use):
    from app.core.redis import redis_client

    with pytest.raises(redis_exceptions.ConnectionError):
        await use(await redis_client())

    assert connects == [UNREACHABLE_HOST]


async def test_a_pubsub_client_is_refused(connects):
    from app.core.redis import redis_pubsub_client

    client = await redis_pubsub_client()
    try:
        with pytest.raises(redis_exceptions.ConnectionError):
            await client.pubsub().subscribe("runner-events")
    finally:
        await client.aclose()

    assert connects == [UNREACHABLE_HOST]


def test_the_pubsub_client_shares_the_refusing_pool():
    import app.core.redis as core_redis

    assert core_redis._shared_client.connection_pool is core_redis._pool
    assert core_redis._pool.connection_class is RefusedConnection


@pytest.mark.parametrize(
    "url",
    [
        # The SAQ queue's own client and any ad-hoc one. An unresolvable host:
        # should the guard ever be off, nothing is written to a real server.
        pytest.param(f"redis://{UNREACHABLE_HOST}:6379/0", id="tcp"),
        pytest.param(f"rediss://{UNREACHABLE_HOST}:6380/0", id="tls"),
        pytest.param("unix:///tmp/redis.sock", id="unix-socket"),
    ],
)
async def test_a_client_built_outside_the_shared_pool_is_refused(url):
    client = redis_asyncio.from_url(url)
    try:
        with pytest.raises(redis_exceptions.ConnectionError, match=REFUSAL):
            await client.set("saq:job", "1")
    finally:
        await client.aclose()


async def test_a_sentinel_finds_no_master(monkeypatch):
    refused: set[tuple[str, int, str]] = set()
    refuse = Connection._connect

    async def _spy(self):
        try:
            await refuse(self)
        except OSError as exc:
            refused.add((self.host, self.port, exc.strerror))
            raise

    monkeypatch.setattr(Connection, "_connect", _spy)
    # No retry: the discovery client's default backs off for seconds.
    sentinel = Sentinel([(UNREACHABLE_HOST, 26379)], sentinel_kwargs={"retry": Retry(NoBackoff(), 0)})
    client = sentinel.master_for("xtm-one")

    with pytest.raises(MasterNotFoundError):
        await client.get("any")

    assert refused == {(UNREACHABLE_HOST, 26379, REFUSAL)}
    assert issubclass(MasterNotFoundError, redis_exceptions.ConnectionError)


async def test_a_test_undoing_its_monkeypatch_keeps_the_refusal(monkeypatch):
    # The ``connects`` spy is not used: it patches through this monkeypatch.
    monkeypatch.undo()
    from app.core.redis import redis_client

    shared = await redis_client()
    elsewhere = redis_asyncio.from_url(f"redis://{UNREACHABLE_HOST}:6379/0")
    try:
        for client in (shared, elsewhere):
            with pytest.raises(redis_exceptions.ConnectionError, match=REFUSAL):
                await client.get("any")
    finally:
        await elsewhere.aclose()
    assert shared.connection_pool.connection_class is RefusedConnection


def test_a_patch_that_outlives_the_refusal_leaves_nothing_refusing():
    """A test's monkeypatch is torn down after the guard: what it puts back
    then must not refuse the tests after it."""
    import app.core.redis as core_redis

    late = pytest.MonkeyPatch()  # stands for the test's own monkeypatch
    with redis_unreachable.refusing():  # stands for the guard
        late.setattr(Connection, "_connect", Connection._connect)
        late.setattr(core_redis, "_shared_client", core_redis._shared_client)
        late.setattr(core_redis, "_pool", core_redis._pool)
        refusing_client, refusing_pool = core_redis._shared_client, core_redis._pool
    late.undo()

    # The socket class gets the switch back, not a refusal of its own...
    assert Connection._connect.refuses_in_the_unit_suite is True
    # ...and the refusing client and pool it put back are replaced by real ones.
    assert (core_redis._shared_client, core_redis._pool) == (refusing_client, refusing_pool)
    redis_unreachable.real_redis()
    assert core_redis._shared_client.connection_pool.connection_class is not RefusedConnection
    assert core_redis._pool.connection_class is not RefusedConnection


@pytest.mark.parametrize("patched", ["_shared_client", "_pool"])
def test_a_refusing_client_or_pool_left_alone_is_rebuilt(patched):
    """``redis_pubsub_client()`` builds on ``_pool``, ``redis_client()`` returns
    ``_shared_client``: a refusing one in either place is rebuilt."""
    import app.core.redis as core_redis

    late = pytest.MonkeyPatch()
    with redis_unreachable.refusing():
        refusing = getattr(core_redis, patched)
        late.setattr(core_redis, patched, refusing)
    late.undo()
    assert getattr(core_redis, patched) is refusing  # put back after the guard ended

    redis_unreachable.real_redis()

    assert core_redis._shared_client.connection_pool is core_redis._pool
    assert core_redis._pool.connection_class is not RefusedConnection


def test_a_sync_client_is_refused():
    import redis

    client = redis.from_url(f"redis://{UNREACHABLE_HOST}:6379/0")
    try:
        with pytest.raises(redis_exceptions.ConnectionError, match=REFUSAL):
            client.set("sync:key", "1")
    finally:
        client.close()


@pytest.mark.integration
async def test_an_integration_test_keeps_the_real_redis():
    import app.core.redis as core_redis
    from app.core.config import settings

    assert redis_unreachable.refusing_now() is False
    assert core_redis._shared_client.connection_pool.connection_class is not RefusedConnection
    # A fresh client: the shared pool may hold a connection an earlier test's
    # closed event loop owns. Its socket goes through the session's switch.
    client = redis_asyncio.from_url(settings.redis_url)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
