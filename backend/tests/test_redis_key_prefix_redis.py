"""``REDIS_KEY_PREFIX`` against a real Redis, as a user its ACL limits to the prefix.

It needs ``REDIS_URL`` to reach a Redis it may write to and create an ACL user
on - the integration job's service, or a throwaway container - and skips
otherwise. Every key it writes is under a fresh prefix and deleted afterwards,
and the ACL user it creates is deleted too.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import ResponseError
from saq.job import Status

from app.core import queue as queue_module
from app.core.redis import close_pubsub, open_pubsub
from app.core.redis_key_prefix import prefixed_client_class

pytestmark = pytest.mark.integration


@pytest.fixture
async def restricted(monkeypatch):
    """(prefix, URL of a user limited to the prefix, unrestricted client)."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    admin = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await admin.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await admin.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    token = uuid.uuid4().hex[:12]
    prefix, user, password = f"test-{token}:", f"test-{token}", uuid.uuid4().hex
    try:
        await admin.execute_command(
            "ACL", "SETUSER", user, "on", f">{password}", "resetkeys", "resetchannels",
            f"~{prefix}*", f"&{prefix}*", "+@all",
        )
    except ResponseError as exc:  # pragma: no cover - environment dependent
        await admin.aclose()
        pytest.skip(f"Cannot create an ACL user: {exc}")
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    user_url = urlunsplit((parts.scheme, f"{user}:{password}@{host}", parts.path, parts.query, ""))
    from saq import job as saq_job

    monkeypatch.setattr(saq_job, "ABORT_ID_PREFIX", saq_job.ABORT_ID_PREFIX)
    try:
        yield prefix, user_url, admin
    finally:
        keys = [key async for key in admin.scan_iter(match=f"{prefix}*", count=500)]
        if keys:
            await admin.delete(*keys)
        await admin.execute_command("ACL", "DELUSER", user)
        await admin.aclose()


async def _names(admin: redis_asyncio.Redis, prefix: str) -> set[str]:
    return {key.removeprefix(prefix) async for key in admin.scan_iter(match=f"{prefix}*", count=500)}


async def test_the_client_reads_and_writes_under_the_prefix_only(restricted):
    prefix, user_url, admin = restricted
    client = prefixed_client_class(prefix).from_url(user_url, decode_responses=True)
    try:
        await client.set("t:str", "v", ex=60)
        await client.rpush("t:list", "a")
        assert tuple(await client.blpop(["t:none", "t:list"], timeout=1)) == ("t:list", "a")
        await client.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "t:lua", "1")
        async with client.pipeline(transaction=True) as pipe:
            assert await pipe.incr("t:pipe").expire("t:pipe", 60).execute() == [1, True]
        async with client.lock("t:lock", timeout=5):
            assert await client.exists("t:lock") == 1
        assert sorted([key async for key in client.scan_iter(match="t:*")]) == ["t:lua", "t:pipe", "t:str"]
        assert {key async for key in client.scan_iter()} == {"t:lua", "t:pipe", "t:str"}

        assert await _names(admin, prefix) == {"t:lua", "t:pipe", "t:str"}
    finally:
        await client.aclose()


async def test_subscriptions_and_patterns_work_under_the_prefix(restricted):
    prefix, user_url, _admin = restricted
    client = prefixed_client_class(prefix).from_url(user_url, decode_responses=True)
    subscriber = prefixed_client_class(prefix).from_url(user_url, decode_responses=True)
    pubsub = await open_pubsub(subscriber, "t:chan", patterns=("t:wake:*",))
    try:
        await asyncio.sleep(0.1)
        await client.publish("t:other", "no")
        await client.publish("t:chan", "hello")
        await client.publish("t:wake:1", "1")
        received = []
        for _ in range(30):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                received.append((message["type"], message["pattern"], message["channel"], message["data"]))
            if len(received) == 2:
                break
        assert received == [("message", None, "t:chan", "hello"), ("pmessage", "t:wake:*", "t:wake:1", "1")]
    finally:
        await close_pubsub(pubsub, subscriber)
        await client.aclose()


async def test_the_saq_queue_runs_under_the_prefix(restricted):
    prefix, user_url, admin = restricted
    saq_client = redis_asyncio.from_url(user_url, decode_responses=False)
    queue = queue_module._teardown_safe_queue_cls()(saq_client, key_prefix=prefix)
    try:
        job = await queue.enqueue("noop", key="k1")
        assert job is not None
        dequeued = await queue.dequeue(timeout=1)
        assert dequeued is not None and dequeued.key == "k1"

        async def _finish_later() -> None:
            await asyncio.sleep(0.5)
            await dequeued.finish(Status.COMPLETE, result=1)

        finisher = asyncio.create_task(_finish_later())
        # Listens to the job's channel through SAQ's pattern subscription.
        await job.refresh(until_complete=5)
        await finisher
        assert job.result == 1
        # The abort key is written and deleted as the restricted user.
        await queue.abort(await queue.enqueue("noop", key="k2"), "aborted")
        assert sorted([j.key async for j in queue.iter_jobs()]) == ["k1", "k2"]

        assert await _names(admin, prefix) == {"saq:job:default:k1", "saq:job:default:k2"}
    finally:
        await queue.disconnect()
