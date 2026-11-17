from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FrozenDateTime(datetime):
    _now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._now.replace(tzinfo=None)
        return cls._now.astimezone(tz)


@pytest.fixture(autouse=True)
def _stub_audit_log_purge(monkeypatch):
    """The audit log purge rides the same tick; these fake sessions run no SQL."""
    calls: list[int | None] = []

    async def _fake_audit_purge(_db, retention_days):
        calls.append(retention_days)
        return 0

    monkeypatch.setattr("app.services.audit_retention.purge_expired_audit_logs", _fake_audit_purge)
    return calls


def _load_autonomous_module():
    import app.models.knowledge_base as kb_model

    if not hasattr(kb_model, "KBExportPackage"):
        kb_model.KBExportPackage = object
    sys.modules.pop("app.services.autonomous", None)
    return importlib.import_module("app.services.autonomous")


@pytest.mark.asyncio
async def test_trace_purge_runs_hourly_with_retention_days(monkeypatch):
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    calls: list[int] = []
    ps = SimpleNamespace(trace_retention_days=45, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        calls.append(retention_days)
        return 5

    async def _fake_aggregate(_db):
        return 0

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr("app.services.trace_storage.aggregate_daily_stats", _fake_aggregate)
    monkeypatch.setattr(autonomous, "datetime", _FrozenDateTime)
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", autonomous._TRACE_PURGE_INTERVAL - 1)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", 0)

    await autonomous._run_trace_storage_maintenance(db)

    assert calls == [45]
    assert autonomous._trace_purge_tick_counter == 0


@pytest.mark.asyncio
async def test_purge_tick_runs_both_clickhouse_retention_steps(monkeypatch):
    # ClickHouse needs two things from this tick, in this order: the TTL declared
    # (the DDL runs before any session can read trace_retention_days, and the
    # tables may predate the TTL), and the expired window swept - the sweep is
    # what deletes the offloaded payload objects, which the PostgreSQL purge
    # deliberately leaves alone under this provider. Same cycle as the PG purge:
    # the objects must not outlive the traces that own them.
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    order: list[str] = []
    ps = SimpleNamespace(trace_retention_days=45, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        order.append(f"postgres:{retention_days}")
        return 0

    async def _fake_sync(retention_days: int, **_kwargs) -> list[str]:
        order.append(f"ttl:{retention_days}")
        return ["llm_traces"]

    async def _fake_ch_purge(retention_days: int, **_kwargs) -> int:
        order.append(f"clickhouse:{retention_days}")
        return 3

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr(
        "app.services.trace_storage.sync_clickhouse_trace_retention", _fake_sync
    )
    monkeypatch.setattr(
        "app.services.trace_storage.purge_expired_clickhouse_traces", _fake_ch_purge
    )
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", autonomous._TRACE_PURGE_INTERVAL - 1)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", 0)

    await autonomous._run_trace_storage_maintenance(db)

    assert order == ["postgres:45", "ttl:45", "clickhouse:45"]


@pytest.mark.asyncio
async def test_daily_stats_backfill_runs_when_due(monkeypatch):
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    backfill_calls: list[int] = []
    ps = SimpleNamespace(trace_retention_days=90, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        _ = retention_days
        return 0

    async def _fake_backfill(_db, *, retention_days: int, **_kwargs) -> int:
        backfill_calls.append(retention_days)
        return 3

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr("app.services.trace_storage.run_daily_stats_backfill", _fake_backfill)
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", 0)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", autonomous._DAILY_STATS_INTERVAL - 1)

    await autonomous._run_trace_storage_maintenance(db)

    assert backfill_calls == [90]
    assert autonomous._daily_stats_tick_counter == 0


@pytest.mark.asyncio
async def test_daily_stats_backfill_fires_on_first_tick_after_restart(monkeypatch):
    # The counters are seeded AT their interval at import time, so a freshly
    # (re)started replica runs the backfill on its very first maintenance tick
    # instead of waiting a full interval - the regression that left
    # last_aggregation_at NULL ("stats may be incomplete") on a platform that
    # restarted more often than the interval.
    autonomous = _load_autonomous_module()
    assert autonomous._daily_stats_tick_counter == autonomous._DAILY_STATS_INTERVAL
    db = _FakeDB()
    backfill_calls = 0
    ps = SimpleNamespace(trace_retention_days=60, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        _ = retention_days
        return 0

    async def _fake_backfill(_db, *, retention_days: int, **_kwargs) -> int:
        nonlocal backfill_calls
        backfill_calls += 1
        return 0

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr("app.services.trace_storage.run_daily_stats_backfill", _fake_backfill)

    await autonomous._run_trace_storage_maintenance(db)

    assert backfill_calls == 1


@pytest.mark.asyncio
async def test_trace_storage_maintenance_rolls_back_on_errors(monkeypatch):
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    ps = SimpleNamespace(trace_retention_days=90, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _failing_purge(_db, retention_days: int) -> int:
        _ = retention_days
        raise RuntimeError("purge failed")

    async def _fake_aggregate(_db):
        return 0

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _failing_purge)
    monkeypatch.setattr("app.services.trace_storage.aggregate_daily_stats", _fake_aggregate)
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", autonomous._TRACE_PURGE_INTERVAL - 1)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", 0)

    await autonomous._run_trace_storage_maintenance(db)

    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_audit_log_purge_runs_hourly_with_its_own_retention(monkeypatch, _stub_audit_log_purge):
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    ps = SimpleNamespace(trace_retention_days=7, audit_log_retention_days=120, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        _ = retention_days
        return 0

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", autonomous._TRACE_PURGE_INTERVAL - 1)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", 0)

    await autonomous._run_trace_storage_maintenance(db)
    assert _stub_audit_log_purge == [120]

    # Not on the next tick: hourly, with the trace purge.
    await autonomous._run_trace_storage_maintenance(db)
    assert _stub_audit_log_purge == [120]


@pytest.mark.asyncio
async def test_a_failed_trace_purge_still_runs_the_audit_log_purge(monkeypatch, _stub_audit_log_purge):
    autonomous = _load_autonomous_module()
    db = _FakeDB()
    ps = SimpleNamespace(trace_retention_days=90, audit_log_retention_days=90, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _failing_purge(_db, retention_days: int) -> int:
        _ = retention_days
        raise RuntimeError("purge failed")

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _failing_purge)
    monkeypatch.setattr(autonomous, "_trace_purge_tick_counter", autonomous._TRACE_PURGE_INTERVAL - 1)
    monkeypatch.setattr(autonomous, "_daily_stats_tick_counter", 0)

    await autonomous._run_trace_storage_maintenance(db)

    assert _stub_audit_log_purge == [90]


@pytest.mark.asyncio
async def test_backfill_runs_before_the_purge_on_the_same_tick(monkeypatch):
    # Both counters are seeded AT their interval, so the first tick after every
    # startup fires both blocks. With the purge first, the oldest day in the
    # window was aggregated from an already-truncated set of traces and the
    # resulting rollup - which aggregate_daily_stats never rewrites - silently
    # under-reported forever. The rollup is the only thing that outlives the
    # purge, so it must be built first.
    autonomous = _load_autonomous_module()
    assert autonomous._daily_stats_tick_counter == autonomous._DAILY_STATS_INTERVAL
    assert autonomous._trace_purge_tick_counter == autonomous._TRACE_PURGE_INTERVAL
    db = _FakeDB()
    order: list[str] = []
    ps = SimpleNamespace(trace_retention_days=7, last_aggregation_at=None)

    async def _fake_get_platform_settings(_db):
        return ps

    async def _fake_purge(_db, retention_days: int) -> int:
        _ = retention_days
        order.append("purge")
        return 0

    async def _fake_backfill(_db, *, retention_days: int, **_kwargs) -> int:
        _ = retention_days
        order.append("backfill")
        return 0

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_get_platform_settings,
    )
    monkeypatch.setattr("app.services.trace_storage.purge_expired_traces", _fake_purge)
    monkeypatch.setattr("app.services.trace_storage.run_daily_stats_backfill", _fake_backfill)

    await autonomous._run_trace_storage_maintenance(db)

    assert order == ["backfill", "purge"]
