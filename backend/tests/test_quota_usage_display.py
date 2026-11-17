"""Tests for get_user_quota_usage() display entries.

Covers the no-config fallback: when no quota config resolves for a
quota type (``sources.* = "none"``, i.e. unlimited / PoC mode), the function
must still return the default monthly counters read from Redis instead of
skipping the quota type — otherwise UI usage indicators (composer counter,
profile gauges) would show nothing while counters keep incrementing.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services import quota as quota_module
from app.services.quota import (
    _current_period_key,
    _usage_key,
    get_platform_quota_monitoring,
    get_user_quota_usage,
    resolve_full_quota_info,
)


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeEmptyResult:
    """A DB result stub answering every access pattern the monitoring
    function's various queries use, all as "nothing configured/found"."""

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return 0


class _FakeConfiguredResult:
    """Like _FakeEmptyResult, but scalar_one_or_none() returns *value* —
    used to simulate PlatformSettings.quota_config being set."""

    def __init__(self, value):
        self._value = value

    def all(self):
        return []

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return 0


class _FakeMonitoringRedis:
    """Redis fake with no cached counters — every scan is empty."""

    def __init__(self):
        self.get = AsyncMock(return_value=None)

    async def scan_iter(self, match=None, count=None):
        return
        yield  # pragma: no cover - makes this an async generator


def _make_fake_redis(counts: dict[str, bytes]) -> MagicMock:
    """Return a Redis fake whose ``get`` serves from *counts* (else None)."""
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda key: counts.get(key))
    redis.set = AsyncMock(return_value=True)
    return redis


def _make_db_with_user_cfg(user_cfg) -> MagicMock:
    """DB fake whose single ``execute(...).scalar_one_or_none()`` returns the
    user's ``quota_config`` (as ``resolve_full_quota_info`` reads it)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user_cfg)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


async def test_get_user_quota_usage_returns_monthly_entries_without_config(monkeypatch):
    """No resolved config -> both quota types fall back to the monthly bucket.

    This mirrors the write side: ``increment_agentic_usage`` /
    ``increment_llm_usage`` default to ``period="monthly"`` when
    ``resolve_quota_config`` returns None, so the read side must query the
    same bucket.
    """
    user_id = uuid.uuid4()
    monthly_key = _current_period_key("monthly")
    counts = {
        _usage_key(user_id, "agentic", None, monthly_key): b"9",
        _usage_key(user_id, "llm_proxy", None, monthly_key): b"3",
        _usage_key(user_id, "sandbox", None, monthly_key): b"5",
    }

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", AsyncMock(return_value=None)
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    assert {(e["usage_type"], e["model"], e["period"], e["count"]) for e in entries} == {
        ("llm_proxy", None, monthly_key, 3),
        ("agentic", None, monthly_key, 9),
        ("sandbox", None, monthly_key, 5),
    }


async def test_get_user_quota_usage_zero_counts_without_config_or_usage(monkeypatch):
    """No config AND no Redis keys -> zero-count entries, not an empty list."""
    user_id = uuid.uuid4()

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis({}))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", AsyncMock(return_value=None)
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    assert len(entries) == 3
    assert all(e["count"] == 0 and e["model"] is None for e in entries)
    assert {e["usage_type"] for e in entries} == {"llm_proxy", "agentic", "sandbox"}


async def test_get_user_quota_usage_respects_configured_period_and_per_model(monkeypatch):
    """With a config, the configured period bucket is queried and llm_proxy
    per-model counters are included."""
    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        _usage_key(user_id, "llm_proxy", None, daily_key): b"5",
        _usage_key(user_id, "llm_proxy", "gpt-test", daily_key): b"2",
        _usage_key(user_id, "agentic", None, daily_key): b"7",
    }

    config = {"period": "daily", "default": 10, "per_model": {"gpt-test": 4}}
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", AsyncMock(return_value=config)
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    assert {(e["usage_type"], e["model"], e["period"], e["count"]) for e in entries} == {
        ("llm_proxy", None, daily_key, 5),
        ("llm_proxy", "gpt-test", daily_key, 2),
        ("agentic", None, daily_key, 7),
        ("sandbox", None, daily_key, 0),
    }


async def test_llm_proxy_quota_usage_reads_the_gate_numbers_and_moves_nothing(monkeypatch):
    """``GET /v1/quota``: the counts ``check_llm_proxy_quota`` compares, read only (a lab retest's proof)."""
    from datetime import UTC, datetime

    from app.services.quota import llm_proxy_quota_usage

    user_id = uuid.uuid4()
    now = datetime(2026, 11, 11, 2, 30, tzinfo=UTC)
    counts = {
        _usage_key(user_id, "llm_proxy", None, "2026-11-11"): b"200",
        _usage_key(user_id, "llm_proxy", "gpt-lab", "2026-11-11"): b"12",
    }
    redis = _make_fake_redis(counts)
    redis.incr = AsyncMock(side_effect=AssertionError("a read moves no counter"))
    config = {"period": "daily", "default": 200, "per_model": {"gpt-lab": 50}}
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=config))

    assert await llm_proxy_quota_usage(MagicMock(), user_id, now=now) == {
        "used": 200,
        "limit": 200,
        "period": "daily",
        "period_key": "2026-11-11",
        "resets_at": "2026-11-12T00:00:00Z",
        "per_model": {"gpt-lab": {"used": 12, "limit": 50}},
    }
    redis.incr.assert_not_awaited()

    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))
    unlimited = await llm_proxy_quota_usage(MagicMock(), user_id, now=now)
    assert (unlimited["limit"], unlimited["period"], unlimited["period_key"]) == (None, "monthly", "2026-11")
    assert unlimited["per_model"] == {}


async def test_get_user_quota_usage_includes_sandbox_entry(monkeypatch):
    """Spec 002-sandbox-quota-usage-tracking FR-013: get_user_quota_usage
    must surface a "sandbox" entry alongside llm_proxy/agentic — reusing
    agentic's resolved period (FR-011, revised), not one of its own."""
    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        _usage_key(user_id, "sandbox", None, daily_key): b"11",
    }

    async def fake_resolve_quota_config(db, redis, uid, qt):
        return {"period": "daily"} if qt == "agentic" else None

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", fake_resolve_quota_config
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    sandbox_entries = [e for e in entries if e["usage_type"] == "sandbox"]
    assert sandbox_entries == [
        {"usage_type": "sandbox", "model": None, "period": daily_key, "count": 11}
    ]


async def test_resolve_full_quota_info_never_resolves_sandbox(monkeypatch):
    """Sandbox has no config of its own to resolve (spec FR-011, revised —
    it always shares agentic's) — a stray "sandbox" key at any level (user,
    group, or global — e.g. left over from before this change) must not
    appear in resolved_config or sources."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    user_id = uuid.uuid4()
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_FakeScalarResult({"sandbox": {"period": "daily"}})
    )
    redis = MagicMock()
    monkeypatch.setattr(
        quota_module,
        "_load_user_groups_with_quotas",
        AsyncMock(return_value=[{"sandbox": {"period": "yearly", "default": 5}}]),
    )
    monkeypatch.setattr(
        quota_module,
        "_load_global_quota_config",
        AsyncMock(return_value={"sandbox": {"period": "monthly", "platform_limit": 100}}),
    )

    resolved, sources = await resolve_full_quota_info(db, redis, user_id)

    assert "sandbox" not in resolved
    assert "sandbox" not in sources


async def test_resolve_full_quota_info_llm_proxy_agentic_unaffected_by_sandbox(monkeypatch):
    """llm_proxy/agentic resolution and the agentic XTM-license fallback
    must be unaffected by sandbox no longer being independently resolved."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    user_id = uuid.uuid4()
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_FakeScalarResult(
            {
                "llm_proxy": {"default": 10, "period": "daily"},
                "agentic": {"default": 20, "period": "monthly"},
            }
        )
    )
    redis = MagicMock()
    monkeypatch.setattr(
        quota_module, "_load_user_groups_with_quotas", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=None)
    )

    resolved, sources = await resolve_full_quota_info(db, redis, user_id)

    assert resolved["llm_proxy"] == {"default": 10, "period": "daily"}
    assert sources["llm_proxy"] == "user"
    assert resolved["agentic"] == {"default": 20, "period": "monthly"}
    assert sources["agentic"] == "user"


async def test_get_platform_quota_monitoring_includes_sandbox_summary_unconditionally(
    monkeypatch,
):
    """Spec 002-sandbox-quota-usage-tracking FR-013/FR-014: the platform
    monitoring dashboard must show a sandbox row even when nothing anywhere
    configures a sandbox quota — unlike agentic/llm_proxy, whose summary
    rows are conditionally emitted based on configured limits."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_FakeEmptyResult())
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_FakeMonitoringRedis())
    )

    result = await get_platform_quota_monitoring(db)

    sandbox_rows = [q for q in result["quotas"] if q["type"] == "sandbox"]
    assert len(sandbox_rows) == 1
    sandbox_row = sandbox_rows[0]
    assert sandbox_row["limit"] is None
    assert sandbox_row["is_global"] is True
    assert sandbox_row["current_usage"] == 0
    assert sandbox_row["active_users"] == 0


async def test_get_platform_quota_monitoring_sandbox_summary_always_monthly(
    monkeypatch,
):
    """The sandbox platform-monitoring row has no configurable period of its
    own (spec FR-011, revised) — it always scans "monthly", regardless of
    any stray "sandbox" key left in a stored platform config (e.g. from
    before this change)."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_FakeConfiguredResult({"sandbox": {"period": "daily"}})
    )
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_FakeMonitoringRedis())
    )

    result = await get_platform_quota_monitoring(db)

    sandbox_rows = [q for q in result["quotas"] if q["type"] == "sandbox"]
    assert len(sandbox_rows) == 1
    assert sandbox_rows[0]["period"] == "monthly"


async def test_get_platform_quota_monitoring_reads_both_zeros_like_the_gate(
    monkeypatch,
):
    """As stored: a ``platform_limit`` of 0 is no platform cap
    (no platform-wide row), while a per-user default of 0 is a hard stop,
    reported as a limit of 0 rather than as unlimited."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_FakeConfiguredResult(
            {"agentic": {"platform_limit": 0, "default": 0, "period": "monthly"}}
        )
    )
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_FakeMonitoringRedis())
    )

    result = await get_platform_quota_monitoring(db)

    agentic_rows = [q for q in result["quotas"] if q["type"] == "agentic"]
    assert [(q["limit"], q["is_global"]) for q in agentic_rows] == [(0, False)]


# ---------------------------------------------------------------------------
# Shared platform-wide pool: a ``global`` agentic config is enforced
# against the global counter, so the display must report that counter too.
# ---------------------------------------------------------------------------


async def test_get_user_quota_usage_reports_shared_pool_for_global_config(monkeypatch):
    """A ``global`` agentic config must report the shared platform-wide pool
    (the global counter), not the viewer's per-user slice, tagged
    ``scope="global"``.

    This is the XTM One included-allowance shape (Enterprise Edition inherited
    from a connected EE platform): ``check_agentic_quota`` gates on
    the global counter and returns early on ``config.get("global")``, skipping
    the per-user limit entirely — so the viewer's own slice is not what gates
    them, and showing it made the composer gauge drift from both enforcement
    and the admin monitoring view.
    """
    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        # The viewer's own slice — must NOT be what gets reported.
        _usage_key(user_id, "agentic", None, daily_key): b"2",
        # The shared pool — what actually gates the user.
        quota_module._global_usage_key("agentic", daily_key): b"57",
    }

    async def fake_resolve_quota_config(db, redis, uid, qt):
        if qt == "agentic":
            return {"period": "daily", "default": 100, "global": True}
        return None

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", fake_resolve_quota_config
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    agentic = [e for e in entries if e["usage_type"] == "agentic"]
    assert agentic == [
        {
            "usage_type": "agentic",
            "model": None,
            "period": daily_key,
            "count": 57,
            "scope": "global",
        }
    ]


async def test_the_shared_pool_includes_global_sandbox_usage(monkeypatch):
    """The gauge numerator is what the gate compares: agentic + sandbox.

    It used to report the agentic counter alone, so a platform with sandbox
    activity read 80/100 while enforcement already refused at 100.
    """
    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        quota_module._global_usage_key("agentic", daily_key): b"57",
        quota_module._global_usage_key("sandbox", daily_key): b"13",
        # The viewer's own sandbox slice keeps its own entry, untouched.
        _usage_key(user_id, "sandbox", None, daily_key): b"4",
    }

    async def fake_resolve_quota_config(db, redis, uid, qt):
        if qt == "agentic":
            return {"period": "daily", "default": 100, "global": True}
        return None

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(quota_module, "resolve_quota_config", fake_resolve_quota_config)

    entries = await get_user_quota_usage(MagicMock(), user_id)

    agentic = [e for e in entries if e["usage_type"] == "agentic"]
    assert agentic == [
        {"usage_type": "agentic", "model": None, "period": daily_key, "count": 70, "scope": "global"}
    ]
    sandbox = [e for e in entries if e["usage_type"] == "sandbox"]
    assert sandbox[0]["count"] == 4


async def test_the_shared_pool_reads_full_exactly_when_the_gate_refuses(monkeypatch):
    """Same Redis state, both readers: the gauge reaches the ceiling at the
    very call ``check_agentic_quota`` starts refusing."""
    from app.services.quota import QuotaExceededError, check_agentic_quota

    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        quota_module._global_usage_key("agentic", daily_key): b"87",
        quota_module._global_usage_key("sandbox", daily_key): b"13",
    }

    async def fake_resolve_quota_config(db, redis, uid, qt):
        if qt == "agentic":
            return {"period": "daily", "default": 100, "global": True}
        return None

    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(quota_module, "resolve_quota_config", fake_resolve_quota_config)
    monkeypatch.setattr(
        quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value={"daily": 100})
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)
    pool = next(e for e in entries if e["usage_type"] == "agentic")
    assert pool["count"] == 100

    try:
        await check_agentic_quota(MagicMock(), user_id)
    except QuotaExceededError:
        refused = True
    else:
        refused = False
    assert refused


async def test_get_user_quota_usage_tags_per_user_scope_for_non_global_config(
    monkeypatch,
):
    """The genuinely per-user path is unchanged: the viewer's own counter is
    reported and tagged ``scope="user"`` (pinning the default so a future
    change can't silently relabel per-user counters as shared)."""
    user_id = uuid.uuid4()
    daily_key = _current_period_key("daily")
    counts = {
        _usage_key(user_id, "agentic", None, daily_key): b"2",
        # A shared-pool counter exists but must be ignored here — this
        # config is not ``global``, so the per-user slice is what gates.
        quota_module._global_usage_key("agentic", daily_key): b"57",
    }

    async def fake_resolve_quota_config(db, redis, uid, qt):
        return {"period": "daily", "default": 100} if qt == "agentic" else None

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(counts))
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", fake_resolve_quota_config
    )

    entries = await get_user_quota_usage(MagicMock(), user_id)

    agentic = [e for e in entries if e["usage_type"] == "agentic"]
    assert agentic == [
        {
            "usage_type": "agentic",
            "model": None,
            "period": daily_key,
            "count": 2,
            "scope": "user",
        }
    ]


async def test_resolve_full_quota_info_tighter_xtm_ceiling_replaces_user_config(
    monkeypatch,
):
    """A tighter XTM-license ceiling replaces a looser user config — mirroring
    ``resolve_quota_config``'s step-4 ceiling replacement.

    Without this the two resolvers diverge: ``get_user_quota_usage`` reports
    the shared-pool numerator while the overview still shows the looser
    per-user denominator (and its period), which is the numerator/denominator
    mismatch this display path exists to prevent. The internal ``global`` flag
    is an enforcement marker and must be stripped from the displayed config;
    the source is reported as ``"global"`` so the UI labels it platform-wide.
    """
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    user_id = uuid.uuid4()
    db = _make_db_with_user_cfg({"agentic": {"period": "monthly", "default": 500}})
    monkeypatch.setattr(
        quota_module, "_load_user_groups_with_quotas", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        quota_module,
        "_resolve_xtm_agentic_config",
        AsyncMock(return_value={"default": 30, "period": "daily", "global": True}),
    )

    resolved, sources = await resolve_full_quota_info(db, MagicMock(), user_id)

    assert resolved["agentic"] == {"default": 30, "period": "daily"}
    assert sources["agentic"] == "global"


async def test_resolve_full_quota_info_keeps_user_config_tighter_than_ceiling(
    monkeypatch,
):
    """A user config *tighter* than the XTM ceiling is what gates the user, so
    it is left alone (limit, period and ``"user"`` source all preserved)."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    user_id = uuid.uuid4()
    db = _make_db_with_user_cfg({"agentic": {"period": "monthly", "default": 10}})
    monkeypatch.setattr(
        quota_module, "_load_user_groups_with_quotas", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        quota_module,
        "_resolve_xtm_agentic_config",
        AsyncMock(return_value={"default": 30, "period": "daily", "global": True}),
    )

    resolved, sources = await resolve_full_quota_info(db, MagicMock(), user_id)

    assert resolved["agentic"] == {"period": "monthly", "default": 10}
    assert sources["agentic"] == "user"
