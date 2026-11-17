"""Tests for ``refund_agentic_usage``.

A metered request that is ultimately refused before doing any work (e.g. a
Filigran AI extraction with no resolvable enterprise licence) must not be
charged against the agentic quota. ``refund_agentic_usage`` reverses the exact
counters ``increment_agentic_usage`` touched, and floors every counter at zero
so a stray/double refund can never grant free usage.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from app.services import quota as quota_module
from app.services.quota import (
    _current_period_key,
    _global_usage_key,
    _usage_key,
    increment_agentic_usage,
    refund_agentic_usage,
)
from tests.quota_redis_scripts import run_quota_script


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}

    async def get(self, key):
        val = self.store.get(key)
        return None if val is None else str(val).encode()

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        # The quota scripts (the platform-wide charge, the floored refund).
        return run_quota_script(self.store, {}, script, keys_and_args)


async def test_refund_reverses_increment(monkeypatch):
    """increment then refund nets every counter back to zero."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    # db=None keeps period resolution at the default "monthly" so the test
    # exercises the pure counter arithmetic without a DB.
    await increment_agentic_usage(user_id, agent_id=agent_id)
    await refund_agentic_usage(user_id, agent_id=agent_id)

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0
    assert redis.store[_global_usage_key("agentic", period_key)] == 0
    assert (
        redis.store[_usage_key(user_id, "agentic", None, period_key, agent_id)] == 0
    )


async def test_refund_floors_at_zero(monkeypatch):
    """A refund with no prior usage must not drive counters negative (which
    would grant free usage)."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    user_id = uuid.uuid4()
    await refund_agentic_usage(user_id)

    period_key = _current_period_key("monthly")
    # Untouched counters stay absent/zero — never negative.
    assert redis.store.get(_usage_key(user_id, "agentic", None, period_key), 0) == 0
    assert redis.store.get(_global_usage_key("agentic", period_key), 0) == 0


async def test_refund_does_not_underflow_existing_usage(monkeypatch):
    """Two increments then one refund leaves exactly one unit of usage — a
    refund reverses one logical request, never the user's whole history."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    user_id = uuid.uuid4()
    await increment_agentic_usage(user_id)
    await increment_agentic_usage(user_id)
    await refund_agentic_usage(user_id)

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1
    assert redis.store[_global_usage_key("agentic", period_key)] == 1
