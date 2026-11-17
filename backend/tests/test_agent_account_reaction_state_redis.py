"""The Redis state of reactions as feedback on a real Redis.

``state.claim_reaction_refresh`` / ``release_reaction_refresh`` (one read per
burst of notifications), ``schedule_reaction_check(s)`` / ``due_reaction_checks``
(the periodic checks, each due answer claimed by one caller through ``ZREM``)
and ``note_reactions_refused`` / ``reactions_refused`` (a subscription refused
with its changes), with nothing between them and Redis.

Needs ``REDIS_URL`` to reach a Redis it may write to and skips otherwise. The
checks run on a sorted set of their own, and every key is deleted afterwards.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
import redis.asyncio as redis_asyncio

from app.services.agent_accounts import state

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(monkeypatch):
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")

    async def _redis():
        return client

    checks_key = f"test:agent_account:reaction_checks:{uuid.uuid4().hex}"
    account_id = str(uuid.uuid4())
    monkeypatch.setattr(state, "_redis", _redis)
    monkeypatch.setattr(state, "REACTION_CHECKS_KEY", checks_key)
    try:
        yield client, account_id
    finally:
        keys = [checks_key, *[key async for key in client.scan_iter(f"agent_account:*:{account_id}*")]]
        await client.delete(*keys)
        await client.aclose()


async def test_one_notification_of_a_burst_claims_the_read_until_it_releases_it(redis):
    client, account_id = redis
    assert await state.claim_reaction_refresh(account_id, "chat-1", "m1") is True
    assert await state.claim_reaction_refresh(account_id, "chat-1", "m1") is False
    # Another answer, or the same id in another conversation, is another read.
    assert await state.claim_reaction_refresh(account_id, "chat-2", "m1") is True
    key = state._key("reaction_refresh", account_id, "chat-1", "m1")
    assert 0 < await client.ttl(key) <= state.REACTION_COALESCE_SECONDS
    await state.release_reaction_refresh(account_id, "chat-1", "m1")
    assert await client.exists(key) == 0
    assert await state.claim_reaction_refresh(account_id, "chat-1", "m1") is True


async def test_a_due_check_is_handed_to_one_caller_and_a_later_one_waits(redis):
    client, _account_id = redis
    now = time.time()
    await state.schedule_reaction_check("due-1", now - 10)
    await state.schedule_reaction_checks({"due-2": now - 5, "later": now + 600})
    # A later schedule of the same answer replaces the earlier one.
    await state.schedule_reaction_checks({"due-3": now + 600})
    await state.schedule_reaction_checks({"due-3": now - 1})

    first, second = await asyncio.gather(state.due_reaction_checks(10), state.due_reaction_checks(10))
    assert sorted(first + second) == ["due-1", "due-2", "due-3"]
    assert set(first).isdisjoint(second)
    assert await client.zrange(state.REACTION_CHECKS_KEY, 0, -1) == ["later"]
    assert await state.due_reaction_checks(10) == []


async def test_due_checks_come_oldest_first_up_to_the_limit(redis):
    _client, _account_id = redis
    now = time.time()
    await state.schedule_reaction_checks({f"r{i}": now - 100 + i for i in range(5)})
    assert await state.due_reaction_checks(2) == ["r0", "r1"]
    assert await state.due_reaction_checks(10) == ["r2", "r3", "r4"]


async def test_many_answers_are_scheduled_in_batches(redis, monkeypatch):
    client, _account_id = redis
    monkeypatch.setattr(state, "SCHEDULE_BATCH", 3)
    now = time.time()
    await state.schedule_reaction_checks({f"r{i}": now for i in range(7)})
    assert await client.zcard(state.REACTION_CHECKS_KEY) == 7


async def test_a_refused_subscription_is_remembered_for_a_day(redis):
    client, account_id = redis
    resource = "/teams/t1/channels/c1/messages"
    assert await state.reactions_refused(account_id, resource) is None
    await state.note_reactions_refused(account_id, resource, "x" * 2000)
    assert await state.reactions_refused(account_id, resource) == "x" * state.REACTIONS_REFUSED_DETAIL_CHARS
    assert await state.reactions_refused(account_id, "/users/me/chats/getAllMessages") is None
    key = state._key("reactions_refused", account_id, resource)
    assert state.REACTIONS_REFUSED_TTL_SECONDS - 5 < await client.ttl(key) <= state.REACTIONS_REFUSED_TTL_SECONDS
