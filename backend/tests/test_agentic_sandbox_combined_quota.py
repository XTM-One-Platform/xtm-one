"""Tests for ``check_agentic_quota``'s combined agentic+sandbox usage check
(spec 002-sandbox-quota-usage-tracking FR-015-FR-017).

Sandbox usage has no configurable limit of its own; instead, as of
`/speckit-clarify` 2026-08-03, it shares the existing per-user *agentic*
limit as a combined enforcement ceiling. ``check_agentic_quota`` sums a
user's ``"agentic"`` and ``"sandbox"`` counters — both read under agentic's
own resolved period key (FR-017), never sandbox's independently-resolved
one — and compares the sum against the agentic ``default_limit``. This is
exercised identically in both the Copilot and XTM One code paths.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import quota as quota_module
from app.services.quota import (
    QuotaExceededError,
    _current_period_key,
    _global_usage_key,
    _usage_key,
    check_agentic_quota,
)


class _FakeRedis:
    def __init__(self, counts: dict[str, int] | None = None):
        self.store: dict[str, int] = dict(counts or {})

    async def get(self, key):
        val = self.store.get(key)
        return None if val is None else str(val).encode()

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, nx=False, ex=None, **kwargs):
        if nx and key in self.store:
            return None
        self.store[key] = int(value)
        return True


def _seed(redis: _FakeRedis, user_id, usage_type: str, period_key: str, count: int) -> None:
    key = _usage_key(user_id, usage_type, None, period_key)
    redis.store[key] = count


# ── Copilot mode ─────────────────────────────────────────────────────────


async def test_copilot_raises_when_combined_usage_meets_limit(monkeypatch):
    """3 sandbox + 2 agentic = 5, limit 5 -> refused, even though agentic
    alone (2) is under the limit."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(quota_module, "_check_platform_wide_limit", AsyncMock())

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "agentic", period_key, 2)
    _seed(redis, user_id, "sandbox", period_key, 3)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 5, "period": "monthly"}),
    )

    with pytest.raises(QuotaExceededError) as exc:
        await check_agentic_quota(db=object(), user_id=user_id)

    assert exc.value.current == 5
    assert exc.value.limit == 5


async def test_copilot_does_not_raise_when_combined_usage_under_limit(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(quota_module, "_check_platform_wide_limit", AsyncMock())

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "agentic", period_key, 2)
    _seed(redis, user_id, "sandbox", period_key, 2)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 5, "period": "monthly"}),
    )

    await check_agentic_quota(db=object(), user_id=user_id)  # must not raise


async def test_copilot_platform_wide_limit_unaffected_by_sandbox_usage(monkeypatch):
    """A large per-user sandbox count must not perturb the platform-wide
    hard-cap check, which runs before and independently of the per-user
    combined comparison."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    platform_wide = AsyncMock()
    monkeypatch.setattr(quota_module, "_check_platform_wide_limit", platform_wide)

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "sandbox", period_key, 10_000)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))

    await check_agentic_quota(db=object(), user_id=user_id)

    platform_wide.assert_awaited_once()
    call_args = platform_wide.await_args.args
    assert call_args[2] == "agentic"


# ── XTM One mode ─────────────────────────────────────────────────────────


async def test_xtm_one_raises_when_combined_usage_meets_limit(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value=None))

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "agentic", period_key, 1)
    _seed(redis, user_id, "sandbox", period_key, 4)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 5, "period": "monthly"}),
    )

    with pytest.raises(QuotaExceededError) as exc:
        await check_agentic_quota(db=object(), user_id=user_id)

    assert exc.value.current == 5


async def test_xtm_one_does_not_raise_when_combined_usage_under_limit(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value=None))

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "agentic", period_key, 1)
    _seed(redis, user_id, "sandbox", period_key, 1)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 5, "period": "monthly"}),
    )

    await check_agentic_quota(db=object(), user_id=user_id)  # must not raise


async def test_xtm_one_ceiling_loop_unaffected_by_sandbox_usage(monkeypatch):
    """The platform-wide XTM-license ceiling loop iterates a *global*
    counter (not per-user), so a large per-user sandbox count must not
    trip it."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 1000}),
    )

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    _seed(redis, user_id, "sandbox", period_key, 10_000)
    # Global agentic counter (distinct key from the per-user one) stays low.
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))

    await check_agentic_quota(db=object(), user_id=user_id)  # must not raise


# ── Period-window handling (spec FR-017) ─────────────────────────────────


async def test_xtm_one_ceiling_loop_raises_when_global_sandbox_usage_meets_ceiling(monkeypatch):
    """The global sandbox counter (written by increment_sandbox_usage,
    distinct from the per-user one) must count toward the XTM-license
    ceiling — otherwise a user on the default license config (resolved
    config has "global": True, so the per-user branch never runs) could
    exhaust OpenSandbox calls with zero enforcement, reopening the gap the
    removed platform-wide quota used to close."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 10}),
    )

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    redis.store[_global_usage_key("agentic", period_key)] = 3
    redis.store[_global_usage_key("sandbox", period_key)] = 7
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    # Resolved config is the "global" license fallback -> per-user branch
    # returns immediately without ever comparing usage, so the ceiling loop
    # above it is the only thing that can still catch this.
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 10, "period": "monthly", "global": True}),
    )

    with pytest.raises(QuotaExceededError) as exc:
        await check_agentic_quota(db=object(), user_id=user_id)

    assert exc.value.current == 10
    assert exc.value.limit == 10


async def test_xtm_one_ceiling_loop_does_not_raise_when_global_sandbox_usage_under_ceiling(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 10}),
    )

    user_id = uuid.uuid4()
    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    redis.store[_global_usage_key("agentic", period_key)] = 3
    redis.store[_global_usage_key("sandbox", period_key)] = 4
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 10, "period": "monthly", "global": True}),
    )

    await check_agentic_quota(db=object(), user_id=user_id)  # must not raise


async def test_copilot_platform_wide_limit_sums_global_sandbox_usage(monkeypatch):
    """_check_platform_wide_limit("agentic") is the only gate left for a
    dispatch with no attributable user (user_id=None) — it must sum in the
    global sandbox counter, not just the global agentic one."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")

    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    redis.store[_global_usage_key("agentic", period_key)] = 6
    redis.store[_global_usage_key("sandbox", period_key)] = 4
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    async def fake_load_platform_wide_config(db, quota_type):
        if quota_type == "agentic":
            return {"platform_limit": 10, "period": "monthly"}
        return None

    monkeypatch.setattr(
        quota_module, "_load_platform_wide_config", fake_load_platform_wide_config
    )

    with pytest.raises(QuotaExceededError) as exc:
        await quota_module._check_platform_wide_limit(object(), redis, "agentic")

    assert exc.value.current == 10
    assert exc.value.limit == 10


async def test_check_agentic_quota_tolerates_none_user_id_but_still_enforces_platform_wide_cap(
    monkeypatch,
):
    """An unattributable sandbox dispatch (user_id=None, spec Edge Cases)
    must still be checked against the platform-wide hard cap — only the
    per-user limit, which needs a user to resolve a config for, is skipped."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    resolve_mock = AsyncMock()
    monkeypatch.setattr(quota_module, "resolve_quota_config", resolve_mock)

    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    platform_wide = AsyncMock()
    monkeypatch.setattr(quota_module, "_check_platform_wide_limit", platform_wide)

    await check_agentic_quota(db=object(), user_id=None)

    platform_wide.assert_awaited_once()
    # The per-user branch requires a user to resolve a config for — must
    # never be reached when user_id is None.
    resolve_mock.assert_not_awaited()


async def test_xtm_one_ceiling_loop_still_runs_when_user_id_none(monkeypatch):
    """XTM One mode: the ceilings loop (global, user-independent) must still
    run and can still raise even with no attributable user."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 5}),
    )

    period_key = _current_period_key("monthly")
    redis = _FakeRedis()
    redis.store[_global_usage_key("agentic", period_key)] = 2
    redis.store[_global_usage_key("sandbox", period_key)] = 3
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    resolve_mock = AsyncMock()
    monkeypatch.setattr(quota_module, "resolve_quota_config", resolve_mock)

    with pytest.raises(QuotaExceededError):
        await check_agentic_quota(db=object(), user_id=None)

    resolve_mock.assert_not_awaited()


async def test_sandbox_term_is_read_under_agentics_own_period_not_its_own(monkeypatch):
    """Agentic resolves to a daily period while sandbox independently
    resolves to monthly for the same user (FR-011) — the combined check
    must still find the sandbox usage, because it looks it up under
    agentic's (daily) period key, not sandbox's own (monthly) one."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(quota_module, "_check_platform_wide_limit", AsyncMock())

    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    redis = _FakeRedis()
    _seed(redis, user_id, "agentic", daily_key, 1)
    # Seed the sandbox counter ONLY under the daily key: if check_agentic_quota
    # incorrectly re-resolved sandbox's own period (monthly) instead of
    # reusing agentic's (daily), the sandbox lookup below would return 0 and
    # the combined total (1) would stay under the limit (5) — i.e. it would
    # NOT raise. Raising here is proof the daily key was actually consulted.
    _seed(redis, user_id, "sandbox", daily_key, 4)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    # check_agentic_quota only resolves the "agentic" config; it must not
    # re-resolve "sandbox" for this calculation at all.
    monkeypatch.setattr(
        quota_module,
        "resolve_quota_config",
        AsyncMock(return_value={"default": 5, "period": "daily"}),
    )

    with pytest.raises(QuotaExceededError) as exc:
        await check_agentic_quota(db=object(), user_id=user_id)

    assert exc.value.current == 5
