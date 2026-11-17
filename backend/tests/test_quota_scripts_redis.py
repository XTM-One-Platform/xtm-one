"""The quota Lua scripts against a real Redis.

The failure-pool suites run the scripts' Python twins
(``tests/quota_redis_scripts.py``); this module runs the scripts themselves,
so a Lua mistake cannot hide behind a double. It needs ``REDIS_URL`` to reach
a Redis it may write to - the integration job's service, or a throwaway
container - and skips otherwise. Every key it writes carries a fresh user id
and is deleted afterwards; it never advances the platform-wide epoch.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis_asyncio

from app.services import quota

pytestmark = pytest.mark.integration

_WINDOW = "2026-01-01"


@pytest.fixture
async def real_redis(monkeypatch):
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    users: list[uuid.UUID] = []

    async def _client():
        return client

    monkeypatch.setattr(quota, "redis_client", _client)
    try:
        yield client, users
    finally:
        for user in users:
            for pattern in (
                f"quota:usage:{user}:*",
                f"quota:refund_causes:*:{user}*",
                f"quota:refund_epoch:{user}",
            ):
                keys = [key async for key in client.scan_iter(match=pattern, count=500)]
                if keys:
                    await client.delete(*keys)
        await client.aclose()


def _new_user(users: list[uuid.UUID]) -> uuid.UUID:
    user = uuid.uuid4()
    users.append(user)
    return user


async def _claim(client, key: str, user: uuid.UUID, pool: int, baseline: int):
    """The caller's two-phase protocol, with a database read in between."""
    claimed, user_epoch, global_epoch = await quota._claim_pool_slot(
        client, key, user, pool, None
    )
    if claimed < 0:
        await asyncio.sleep(0.001)
        claimed, user_epoch, global_epoch = await quota._claim_pool_slot(
            client, key, user, pool, baseline
        )
    return claimed, user_epoch, global_epoch


async def test_cold_claims_never_grant_past_the_pool(real_redis):
    client, users = real_redis
    user = _new_user(users)
    key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)

    results = await asyncio.gather(*(_claim(client, key, user, 5, 3) for _ in range(30)))

    granted = [claimed for claimed, _u, _g in results if claimed <= 5]
    assert sorted(granted) == [4, 5], "the durable 3 plus two slots, not one more"
    assert int(await client.get(key)) == 5
    assert await client.ttl(key) > 366 * 86_400


async def test_a_warm_claim_is_decided_without_the_baseline(real_redis):
    client, users = real_redis
    user = _new_user(users)
    key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    await client.set(key, 2)

    claimed, _u, _g = await quota._claim_pool_slot(client, key, user, 3, None)
    refused, _u, _g = await quota._claim_pool_slot(client, key, user, 3, None)

    assert (claimed, refused) == (3, 4)
    assert int(await client.get(key)) == 3, "the refused claim was handed back"


async def test_the_scripts_read_the_epochs_a_credit_is_counted_in(real_redis):
    client, users = real_redis
    user = _new_user(users)
    await quota._advance_refund_epoch(client, user)
    pool_key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    free_key = quota._free_refund_key(user, uuid.uuid4(), _WINDOW)

    _claimed, user_epoch, _global = await _claim(client, pool_key, user, 5, 0)
    counted, free_epoch = await quota._count_free_credit(client, free_key, user, 0)

    assert (user_epoch, counted, free_epoch) == (1, 1, 1)


async def test_a_lost_free_counter_counts_on_from_its_durable_row(real_redis):
    """A free-credit counter Redis lost after its flush is not recreated from
    zero: the caller comes back with the row, so the card's total keeps
    counting with the cause tally instead of stalling below it."""
    client, users = real_redis
    user = _new_user(users)
    free_key = quota._free_refund_key(user, uuid.uuid4(), _WINDOW)

    assert await quota._count_free_credit(client, free_key, user) == (-1, 0)
    assert await client.exists(free_key) == 0
    assert (await quota._count_free_credit(client, free_key, user, 5))[0] == 6
    assert (await quota._count_free_credit(client, free_key, user))[0] == 7
    assert await client.ttl(free_key) > 366 * 86_400


async def test_a_lost_platform_wide_counter_is_never_recreated_from_zero(real_redis):
    """Without its saved mark the charge script counts nothing and creates
    nothing; given the marks, a lost counter counts on from its mark and a held
    one keeps its own count."""
    client, _users = real_redis
    usage_type = f"t4711{uuid.uuid4().hex[:8]}"
    monthly = quota._global_usage_key(usage_type, quota._current_period_key("monthly"))
    yearly = quota._global_usage_key(usage_type, quota._current_period_key("yearly"))
    ttl = quota._USAGE_KEY_TTL
    try:
        await client.set(monthly, 4)
        assert await client.eval(quota._COUNT_GLOBAL_LUA, 2, monthly, yearly, ttl, "", "") == -1
        assert int(await client.get(monthly)) == 4
        assert await client.exists(yearly) == 0

        assert await client.eval(quota._COUNT_GLOBAL_LUA, 2, monthly, yearly, ttl, 99, 40) == 2
        assert (int(await client.get(monthly)), int(await client.get(yearly))) == (5, 41)
        assert await client.ttl(yearly) > 30 * 86_400
        assert await client.eval(quota._COUNT_GLOBAL_LUA, 1, yearly, ttl, "") == 1
        assert int(await client.get(yearly)) == 42
    finally:
        await client.delete(monthly, yearly)


async def test_an_epoch_is_never_left_without_an_expiry(real_redis):
    client, users = real_redis
    user = _new_user(users)

    assert await quota._advance_refund_epoch(client, user) == 1
    assert await client.ttl(quota._refund_epoch_key(user)) > 366 * 86_400


async def test_a_credit_keeps_the_epoch_it_is_filed_under_alive(real_redis):
    """An epoch is set by an admin action, possibly a year ago; the tallies
    filed under it are refreshed by every credit. Were the epoch to expire
    first, the card would read the current window's causes as not recorded."""
    client, users = real_redis
    user = _new_user(users)
    await quota._advance_refund_epoch(client, user)
    epoch_key = quota._refund_epoch_key(user)
    pool_key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    free_key = quota._free_refund_key(user, uuid.uuid4(), _WINDOW)

    await client.expire(epoch_key, 60)
    await _claim(client, pool_key, user, 5, 0)
    assert await client.ttl(epoch_key) > 366 * 86_400

    await client.expire(epoch_key, 60)
    await quota._count_free_credit(client, free_key, user, 0)
    assert await client.ttl(epoch_key) > 366 * 86_400


async def test_a_credit_never_creates_an_epoch(real_redis):
    """Epoch 0 is the absent key, the tally shape: keeping epochs alive
    must not write one for a user nobody reset."""
    client, users = real_redis
    user = _new_user(users)

    await _claim(client, quota._failure_pool_key(user, uuid.uuid4(), _WINDOW), user, 5, 0)
    await quota._count_free_credit(
        client, quota._free_refund_key(user, uuid.uuid4(), _WINDOW), user, 0
    )

    assert await client.exists(quota._refund_epoch_key(user)) == 0


async def test_a_tally_after_a_reset_is_filed_where_nothing_reads(real_redis):
    """Two failures of one pair straddle a reset; the second takes the fresh
    counter back to the first one's value, so the counter check alone would
    let the first, stale tally through."""
    client, users = real_redis
    user = _new_user(users)
    pool = quota._POOL_USAGE_TYPE
    key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)

    stale, stale_user_epoch, stale_global = await _claim(client, key, user, 5, 0)
    # The reset: the counters go, then the user's epoch moves.
    await client.delete(key)
    await quota._advance_refund_epoch(client, user)
    fresh, fresh_user_epoch, fresh_global = await _claim(client, key, user, 5, 0)
    for cause, granted_at, user_epoch, global_epoch in (
        ("provider_overloaded", fresh, fresh_user_epoch, fresh_global),
        ("tool_error", stale, stale_user_epoch, stale_global),
    ):
        await quota._record_refund_cause(
            client,
            pool,
            _WINDOW,
            cause,
            user_id=user,
            counter_key=key,
            granted_at=granted_at,
            epoch_token=quota._cause_epoch_token(pool, user_epoch, global_epoch),
        )

    assert stale == fresh == 1
    assert await quota.read_refund_causes(pool, _WINDOW, user_ids=[user]) == {
        "provider_overloaded": 1
    }


async def test_a_stashed_counter_comes_back_with_its_expiry(real_redis):
    """A clear moves its counters out of the live keyspace until it commits;
    a clear that did not commit puts them back as they were."""
    client, users = real_redis
    user = _new_user(users)
    token = f"test-{uuid.uuid4().hex}"
    stash = quota._clear_stash_key(token)
    pool_key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    usage_key = quota._usage_key(user, "agentic", None, "2026-09")
    await client.set(pool_key, 3, ex=3600)
    await client.set(usage_key, 7)
    try:
        moved = await quota._stash_counters(
            client, token, [pool_key, usage_key, f"{usage_key}:gone"], {}
        )
        assert moved == 2
        assert await client.exists(pool_key, usage_key) == 0
        assert await client.ttl(stash) > 29 * 86_400

        assert await quota._restore_clear_stash(client, token) == 2
        assert int(await client.get(pool_key)) == 3
        assert 3000 < await client.ttl(pool_key) <= 3600
        assert int(await client.get(usage_key)) == 7
        assert await client.ttl(usage_key) == -1
        assert await client.exists(stash) == 0
    finally:
        await client.delete(stash)


async def test_a_counter_recreated_meanwhile_gets_the_stash_on_top_of_it(real_redis):
    """Between the clear and the restore a run bumped a usage counter from
    nothing, and a cold claim seeded the pool counter from its durable row
    once the clear's locks were gone: the restore adds what each is missing,
    and never counts the seed twice."""
    client, users = real_redis
    user = _new_user(users)
    token = f"test-{uuid.uuid4().hex}"
    stash = quota._clear_stash_key(token)
    pool_key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    usage_key = quota._usage_key(user, "agentic", None, "2026-09")
    await client.set(pool_key, 3)
    await client.set(usage_key, 7)
    try:
        await quota._stash_counters(client, token, [pool_key, usage_key], {pool_key: 1})
        await client.set(pool_key, 2)  # the durable 1, plus one claim
        await client.incr(usage_key)

        assert await quota._restore_clear_stash(client, token) == 2
        assert int(await client.get(pool_key)) == 4
        assert int(await client.get(usage_key)) == 8
    finally:
        await client.delete(stash)


async def test_a_counter_moved_twice_adds_up_in_the_stash(real_redis):
    """A scan can return a key twice, and a counter can be recreated while the
    clear runs: the second move adds what the counter gained over its seed,
    which the first count already holds."""
    client, users = real_redis
    user = _new_user(users)
    token = f"test-{uuid.uuid4().hex}"
    stash = quota._clear_stash_key(token)
    usage_key = quota._usage_key(user, "agentic", None, "2026-09")
    pool_key = quota._failure_pool_key(user, uuid.uuid4(), _WINDOW)
    seeds = {pool_key: 1}
    try:
        await client.set(usage_key, 2)
        await client.set(pool_key, 3)
        await quota._stash_counters(client, token, [usage_key, pool_key], seeds)
        await client.set(usage_key, 5)
        await client.set(pool_key, 2)  # the durable 1, plus one claim
        await quota._stash_counters(client, token, [usage_key, pool_key], seeds)

        assert await quota._restore_clear_stash(client, token) == 2
        assert int(await client.get(usage_key)) == 7
        assert int(await client.get(pool_key)) == 4
    finally:
        await client.delete(stash)


async def test_a_receipt_is_redeemed_once_and_its_token_stays_closed(real_redis):
    """Whoever credits a charge redeems its receipt first, and the
    token stays closed for the receipt's lifetime - the attempt that charged it
    and the re-run that replaced that attempt can never both credit it, and a
    charge landing under a closed token is refused its receipt."""
    client, _users = real_redis
    token = f"sub_agent_task:{uuid.uuid4()}:1"
    key = quota._receipt_key(token)
    ttl = quota._RECEIPT_TTL
    redeemed = quota._RECEIPT_REDEEMED
    receipt = '{"user_id": null}'

    async def _write() -> int:
        return await client.eval(quota._WRITE_RECEIPT_LUA, 1, key, receipt, ttl, redeemed)

    try:
        assert await _write() == 1
        assert await quota._redeem_receipt(client, token) == (quota.RECEIPT_TAKEN, receipt)
        assert await client.get(key) == redeemed
        assert 0 < await client.ttl(key) <= ttl, "the receipt's own expiry is kept"
        assert await quota._redeem_receipt(client, token) == (quota.RECEIPT_ALREADY_REDEEMED, None)
        assert await _write() == 0
        assert await client.get(key) == redeemed

        # Redeemed before anything was charged: closed all the same.
        await client.delete(key)
        assert await quota._redeem_receipt(client, token) == (quota.RECEIPT_ABSENT, None)
        assert await client.get(key) == redeemed
        assert 0 < await client.ttl(key) <= ttl
        assert await _write() == 0
    finally:
        await client.delete(key)


async def test_the_refund_paths_tally_what_the_card_reads(real_redis, monkeypatch):
    """The whole credit-back path, end to end on Redis: pool credits up to the
    pool size, the next one refused, a free credit beside them."""
    client, users = real_redis
    user = _new_user(users)
    agent = uuid.uuid4()
    monkeypatch.setattr(quota.settings, "agent_failure_quota_refund_enabled", True)
    monkeypatch.setattr(quota.settings, "agent_failure_quota_pool", 2)
    monkeypatch.setattr(
        quota, "_resolve_agentic_periods", AsyncMock(return_value=("monthly", set()))
    )

    granted = [
        await quota.refund_agentic_usage_for_failure(user, agent, cause="tool_error")
        for _ in range(3)
    ]
    assert await quota.refund_agentic_usage_for_provider_outage(
        user, agent, cause="provider_unreachable"
    )

    from app.services.agent_failure_pool import resolve_pool_window

    window = (await resolve_pool_window(None)).key
    assert granted == [True, True, False]
    assert int(await client.get(quota._failure_pool_key(user, agent, window))) == 2
    assert await quota.read_refund_causes(
        quota._POOL_USAGE_TYPE, window, user_ids=[user]
    ) == {"tool_error": 2}
    assert await quota.read_refund_causes(
        quota._FREE_REFUND_USAGE_TYPE, window, user_ids=[user]
    ) == {"provider_unreachable": 1}


async def test_a_recorded_charge_is_credited_once_and_floored(real_redis):
    """``_CREDIT_CHARGE_ONCE_LUA``: the counters and the receipt move
    together, once; a counter at zero or missing stays as it is."""
    client, users = real_redis
    user = _new_user(users)
    charged = f"quota:usage:{user}:agentic:charged"
    empty = f"quota:usage:{user}:agentic:empty"
    missing = f"quota:usage:{user}:agentic:missing"
    receipt = f"quota:usage:{user}:receipt"
    await client.set(charged, 2)
    await client.set(empty, 0)

    async def _credit():
        return await client.eval(
            quota._CREDIT_CHARGE_ONCE_LUA, 4, receipt, charged, empty, missing,
            quota._CHARGE_CREDITED, 60,
        )

    assert await _credit() == quota.CHARGE_CREDIT_LANDED
    assert await _credit() == quota.CHARGE_CREDIT_ALREADY
    assert await client.get(charged) == "1"
    assert await client.get(empty) == "0"
    assert await client.exists(missing) == 0
    assert await client.get(receipt) == quota._CHARGE_CREDITED
    assert 0 < await client.ttl(receipt) <= 60
