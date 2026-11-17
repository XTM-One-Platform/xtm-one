"""Tests for ``increment_sandbox_usage`` (spec 002-sandbox-quota-usage-tracking).

Sandbox executions (code interpreter, custom-tool JS/Python) previously left
no per-user trace in ``quota_usage`` — only ``llm_proxy``/``agentic`` usage
was tracked per user. ``increment_sandbox_usage`` adds a third, independent
``usage_type="sandbox"`` counter to that same ledger.

Unlike its siblings ``increment_llm_proxy_usage``/``increment_agentic_usage``,
this function deliberately never raises (a failure to record usage must not
fail an already-completed sandbox call — spec FR-010, `/speckit-clarify`
2026-07-31 Q1). It has no period of its own: it resolves and uses the
*agentic* quota's period when ``db`` is provided, falling back to
``"monthly"`` otherwise (spec FR-011, revised — sandbox has no enforceable
cap and always shares the agentic limit/window as its ceiling), and
maintains no global/platform-wide companion counter.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services import quota as quota_module
from app.services.quota import (
    _current_period_key,
    _usage_key,
    increment_agentic_usage,
    increment_llm_proxy_usage,
    increment_sandbox_usage,
    resolve_quota_config,
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

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        # The platform-wide charge script.
        return run_quota_script(self.store, {}, script, keys_and_args)


class _RaisingRedis:
    """Redis fake whose incr() always raises, for the never-raises contract."""

    async def incr(self, key):
        raise ConnectionError("redis unreachable")

    async def eval(self, script, numkeys, *keys_and_args):
        raise ConnectionError("redis unreachable")

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return None


async def test_increment_sandbox_usage_increments_counter(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)

    user_id = uuid.uuid4()
    await increment_sandbox_usage(user_id)

    period_key = _current_period_key("monthly")
    key = _usage_key(user_id, "sandbox", None, period_key)
    assert redis.store[key] == 1

    await increment_sandbox_usage(user_id)
    assert redis.store[key] == 2


async def test_increment_sandbox_usage_noop_when_disabled(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", True)

    await increment_sandbox_usage(uuid.uuid4())

    assert redis.store == {}


async def test_increment_sandbox_usage_swallows_redis_failure(monkeypatch, caplog):
    redis = _RaisingRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)

    # Must not raise — a failure to record usage must never fail an
    # already-completed sandbox call.
    await increment_sandbox_usage(uuid.uuid4())


async def test_increment_sandbox_usage_distinct_from_llm_proxy_and_agentic(monkeypatch):
    """US3: sandbox usage is a separate, non-overwriting counter."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)

    user_id = uuid.uuid4()
    await increment_llm_proxy_usage(user_id, model=None)
    await increment_agentic_usage(user_id)
    await increment_sandbox_usage(user_id)
    await increment_sandbox_usage(user_id)

    period_key = _current_period_key("monthly")
    llm_key = _usage_key(user_id, "llm_proxy", None, period_key)
    agentic_key = _usage_key(user_id, "agentic", None, period_key)
    sandbox_key = _usage_key(user_id, "sandbox", None, period_key)

    assert redis.store[llm_key] == 1
    assert redis.store[agentic_key] == 1
    assert redis.store[sandbox_key] == 2


async def test_increment_sandbox_usage_no_counter_for_unused_user(monkeypatch):
    """US3: a user who never triggers a sandbox execution has no counter."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)

    user_id = uuid.uuid4()
    await increment_agentic_usage(user_id)

    period_key = _current_period_key("monthly")
    sandbox_key = _usage_key(user_id, "sandbox", None, period_key)
    assert sandbox_key not in redis.store


async def test_increment_sandbox_usage_resolves_period_via_db(monkeypatch):
    """FR-011 (revised): when db is provided, use agentic's resolved period.

    Mocks ``_resolve_agentic_periods`` directly — the single source of
    truth shared with ``increment_agentic_usage`` — rather than
    ``resolve_quota_config``/``get_xtm_agentic_ceilings`` individually, so
    this test doesn't depend on ``settings.is_xtm_one`` or touch a real DB
    via the dedicated session ``_resolve_agentic_periods`` itself would open.
    """
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)
    resolve_periods_mock = AsyncMock(return_value=("daily", set()))
    monkeypatch.setattr(quota_module, "_resolve_agentic_periods", resolve_periods_mock)

    user_id = uuid.uuid4()
    fake_db = object()
    await increment_sandbox_usage(user_id, db=fake_db)

    # Concurrency fix: period resolution runs on a dedicated session opened
    # internally, not the caller's shared `fake_db` (see quota.py's comment
    # on increment_sandbox_usage) — `db` here only signals that resolution
    # should be attempted at all.
    resolve_periods_mock.assert_awaited_once()
    awaited_redis, awaited_db, awaited_user_id, awaited_period = resolve_periods_mock.await_args.args
    assert awaited_db is not fake_db
    assert (awaited_redis, awaited_user_id, awaited_period) == (redis, user_id, "monthly")
    daily_key = _usage_key(user_id, "sandbox", None, _current_period_key("daily"))
    monthly_key = _usage_key(user_id, "sandbox", None, _current_period_key("monthly"))
    assert redis.store[daily_key] == 1
    assert monthly_key not in redis.store


async def test_increment_sandbox_usage_falls_back_to_monthly_when_no_config_resolves(monkeypatch):
    """FR-011: db provided but nothing configured -> still monthly."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)
    monkeypatch.setattr(
        quota_module, "_resolve_agentic_periods", AsyncMock(return_value=("monthly", set()))
    )

    user_id = uuid.uuid4()
    await increment_sandbox_usage(user_id, db=object())

    monthly_key = _usage_key(user_id, "sandbox", None, _current_period_key("monthly"))
    assert redis.store[monthly_key] == 1


async def test_increment_sandbox_usage_writes_global_counter_for_extra_periods(monkeypatch):
    """The global sandbox counter is written for the resolved period AND
    every extra platform-wide/license period (mirroring
    increment_agentic_usage's own global-counter fan-out) — this is what
    lets check_agentic_quota's XTM-ceilings loop and Copilot's
    _check_platform_wide_limit see sandbox usage at all."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)
    monkeypatch.setattr(
        quota_module,
        "_resolve_agentic_periods",
        AsyncMock(return_value=("daily", {"monthly", "yearly"})),
    )

    user_id = uuid.uuid4()
    await increment_sandbox_usage(user_id, db=object())

    from app.services.quota import _global_usage_key

    assert redis.store[_usage_key(user_id, "sandbox", None, _current_period_key("daily"))] == 1
    assert redis.store[_global_usage_key("sandbox", _current_period_key("daily"))] == 1
    assert redis.store[_global_usage_key("sandbox", _current_period_key("monthly"))] == 1
    assert redis.store[_global_usage_key("sandbox", _current_period_key("yearly"))] == 1


async def test_increment_sandbox_usage_writes_only_global_counter_when_user_id_none(monkeypatch):
    """No user_id (unattributable call, spec Edge Cases) -> the per-user
    counter is skipped, but the global counter — the platform-wide cap's
    only remaining gate for this scenario — is still written."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)

    await increment_sandbox_usage(None)

    from app.services.quota import _global_usage_key

    period_key = _current_period_key("monthly")
    assert redis.store == {_global_usage_key("sandbox", period_key): 1}


async def test_increment_sandbox_usage_defaults_to_monthly_when_db_none(monkeypatch):
    """FR-011: db=None (the default) skips resolution entirely -> monthly."""
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module.settings, "disable_sandbox_quota_usage", False)
    resolve_mock = AsyncMock(return_value={"period": "yearly"})
    monkeypatch.setattr(quota_module, "resolve_quota_config", resolve_mock)

    user_id = uuid.uuid4()
    await increment_sandbox_usage(user_id)

    resolve_mock.assert_not_awaited()
    monthly_key = _usage_key(user_id, "sandbox", None, _current_period_key("monthly"))
    assert redis.store[monthly_key] == 1


# ---------------------------------------------------------------------------
# resolve_quota_config no longer recognizes "sandbox" (spec FR-011, revised)
#
# Sandbox has no config or period of its own to resolve — it always shares
# agentic's. A stray "sandbox" key in a stored config dict (e.g. left over
# from before this change) must be silently ignored, not resolved.
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _make_config_redis() -> MagicMock:
    """Redis fake with no cached config (forces a fresh resolution)."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.eval = AsyncMock(return_value=1)
    return redis


async def test_resolve_quota_config_sandbox_always_returns_none(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    user_id = uuid.uuid4()
    db = AsyncMock()
    # A stray "sandbox" key at every level must be ignored, not resolved.
    db.execute = AsyncMock(
        return_value=_FakeScalarResult(
            {
                "sandbox": {"default": 5, "period": "daily"},
                "llm_proxy": {"default": 10, "period": "monthly"},
                "agentic": {"default": 10, "period": "monthly"},
            }
        )
    )
    redis = _make_config_redis()

    config = await resolve_quota_config(db, redis, user_id, "sandbox")

    assert config is None


async def test_resolve_quota_config_llm_proxy_agentic_unaffected_by_stray_sandbox_key(monkeypatch):
    """A leftover "sandbox" key in a stored config dict must not affect llm_proxy/agentic resolution."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    user_id = uuid.uuid4()
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_FakeScalarResult(
            {
                "llm_proxy": {"default": 10, "period": "daily"},
                "agentic": {"default": 20, "period": "yearly"},
                "sandbox": {"default": 5, "period": "monthly"},
            }
        )
    )
    redis = _make_config_redis()

    llm_config = await resolve_quota_config(db, redis, user_id, "llm_proxy")
    agentic_config = await resolve_quota_config(db, redis, user_id, "agentic")

    assert llm_config == {"default": 10, "period": "daily"}
    assert agentic_config == {"default": 20, "period": "yearly"}
