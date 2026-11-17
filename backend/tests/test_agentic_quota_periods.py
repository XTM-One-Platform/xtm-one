"""The rows of the composer's quota panel: today, this month, this year.

``get_agentic_quota_periods`` reads, per period, what gates the user there:
their own limit in the period it is set in, else a platform-wide cap (shared
pool), else nothing (their own count, unlimited). Pinned on a frozen clock
with an in-memory Redis.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.services import quota

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 11, 12, 19, 0, tzinfo=UTC)


class _Redis:
    def __init__(self, values: dict[str, int] | None = None) -> None:
        self.values = {k: str(v) for k, v in (values or {}).items()}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = str(value)
        return True


def _user_key(usage_type: str, period_key: str) -> str:
    return quota._usage_key(USER, usage_type, None, period_key)


def _global_key(usage_type: str, period_key: str) -> str:
    return quota._global_usage_key(usage_type, period_key)


@pytest.fixture
def setup(monkeypatch):
    def _apply(values=None, config=None, xtm_one=False, ceilings=None, platform_cfg=None):
        redis = _Redis(values)
        monkeypatch.setattr(quota, "redis_client", AsyncMock(return_value=redis))
        monkeypatch.setattr(quota, "resolve_quota_config", AsyncMock(return_value=config))
        monkeypatch.setattr(quota.settings, "platform_mode", "xtm_one" if xtm_one else "copilot")
        monkeypatch.setattr(quota, "get_xtm_agentic_ceilings", AsyncMock(return_value=ceilings))
        monkeypatch.setattr(quota, "_load_platform_wide_config", AsyncMock(return_value=platform_cfg))
        # A counter Redis does not hold reads its saved mark: none here.
        monkeypatch.setattr(quota, "_load_global_baseline", AsyncMock(return_value=0))
        return redis

    return _apply


def _by_period(rows):
    assert [r["period"] for r in rows] == ["daily", "monthly", "yearly"]
    return {r["period"]: r for r in rows}


async def test_unlimited_user_sees_their_own_count_in_every_period(setup):
    setup(
        values={
            _user_key("agentic", "2026-11"): 2,
            _user_key("sandbox", "2026-11"): 1,
            _user_key("agentic_view", "2026-11-12"): 1,
            _user_key("agentic_view", "2026"): 40,
        }
    )
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["daily"] == {
        "period": "daily",
        "used": 1,
        "limit": None,
        "scope": "user",
        "resets_at": "2026-11-13T00:00:00Z",
    }
    # The resolved period (monthly by default) reads the enforced counters.
    assert rows["monthly"]["used"] == 3 and rows["monthly"]["limit"] is None
    assert rows["monthly"]["resets_at"] == "2026-12-01T00:00:00Z"
    assert rows["yearly"]["used"] == 40
    assert rows["yearly"]["resets_at"] == "2027-01-01T00:00:00Z"


async def test_own_daily_limit_reads_the_gate_numerator(setup):
    setup(
        values={
            _user_key("agentic", "2026-11-12"): 1500,
            _user_key("sandbox", "2026-11-12"): 20,
            _user_key("agentic_view", "2026-11"): 4000,
        },
        config={"default": 100_000, "period": "daily"},
    )
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["daily"]["used"] == 1520
    assert rows["daily"]["limit"] == 100_000 and rows["daily"]["scope"] == "user"
    # No limit in the other periods: the user's own count, unlimited.
    assert rows["monthly"] == {**rows["monthly"], "used": 4000, "limit": None}
    assert rows["yearly"]["used"] == 0 and rows["yearly"]["limit"] is None


async def test_zero_limit_is_a_limit(setup):
    setup(config={"default": 0, "period": "monthly"})
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["monthly"]["limit"] == 0 and rows["monthly"]["used"] == 0


async def test_license_ceilings_show_the_shared_pool(setup):
    setup(
        values={
            _global_key("agentic", "2026-11-12"): 900,
            _global_key("sandbox", "2026-11-12"): 100,
            _global_key("agentic", "2026-11"): 7000,
            _user_key("agentic_view", "2026"): 12,
        },
        config={"default": 1_000_000, "period": "monthly", "global": True},
        xtm_one=True,
        ceilings={"daily": 50_000, "monthly": 1_000_000},
    )
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["daily"] == {**rows["daily"], "used": 1000, "limit": 50_000, "scope": "global"}
    assert rows["monthly"] == {
        **rows["monthly"],
        "used": 7000,
        "limit": 1_000_000,
        "scope": "global",
    }
    # No ceiling sold for the year: the user's own count, unlimited.
    assert rows["yearly"] == {**rows["yearly"], "used": 12, "limit": None, "scope": "user"}


async def test_copilot_platform_limit_is_shared_in_its_period(setup):
    setup(
        values={_global_key("agentic", "2026"): 321},
        platform_cfg={"platform_limit": 10_000, "period": "yearly"},
    )
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["yearly"] == {**rows["yearly"], "used": 321, "limit": 10_000, "scope": "global"}
    assert rows["daily"]["scope"] == "user" and rows["daily"]["limit"] is None


async def test_own_limit_wins_over_the_platform_cap_in_its_period(setup):
    setup(
        values={_user_key("agentic", "2026-11-12"): 5, _global_key("agentic", "2026-11-12"): 99},
        config={"default": 10, "period": "daily"},
        xtm_one=True,
        ceilings={"daily": 50_000},
    )
    rows = _by_period(await quota.get_agentic_quota_periods(None, USER, NOW))
    assert rows["daily"] == {**rows["daily"], "used": 5, "limit": 10, "scope": "user"}


async def test_a_refund_reverses_the_panel_counters(monkeypatch):
    monkeypatch.setattr(quota, "_utcnow", lambda: NOW)
    keys = await quota._agentic_charge_keys(None, None, USER, None, "monthly")
    for period_key in ("2026-11-12", "2026-11", "2026"):
        assert _user_key("agentic_view", period_key) in keys
    # A run with no account charges no panel counter.
    anonymous = await quota._agentic_charge_keys(None, None, None, None, "monthly")
    assert not any("agentic_view" in key for key in anonymous)


async def test_every_panel_period_is_counted_platform_wide():
    period, extra = await quota._resolve_agentic_periods(None, None, None, "monthly")
    assert period == "monthly"
    assert extra == {"daily", "monthly", "yearly"}
