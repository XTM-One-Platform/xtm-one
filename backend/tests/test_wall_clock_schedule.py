"""The wall-clock schedule of the scheduler's hourly jobs (#768).

The hourly maintenance block (retention sweeps, purges, reapers) used to count
ticks in a module global reset at every boot: a platform redeploying more often
than hourly never ran it, and Settings showed a "next sweep" that slid forward
forever. These tests pin the persistent schedule that replaced it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import wall_clock_schedule as wcs

T0 = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
JOB = wcs.HOURLY_MAINTENANCE


class _FakeRedis:
    """Strings with SET NX / EX on a clock the test moves, and the release script."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, datetime] = {}
        self.now = T0

    def _alive(self, key: str) -> bool:
        if key in self.expires and self.expires[key] <= self.now:
            self.values.pop(key, None)
            self.expires.pop(key, None)
        return key in self.values

    async def get(self, key: str):
        return self.values.get(key) if self._alive(key) else None

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and self._alive(key):
            return None
        self.values[key] = value
        if ex:
            self.expires[key] = self.now + timedelta(seconds=ex)
        else:
            self.expires.pop(key, None)
        return True

    async def delete(self, key: str) -> int:
        existed = self._alive(key)
        self.values.pop(key, None)
        self.expires.pop(key, None)
        return int(existed)

    async def eval(self, _script: str, _n: int, key: str, token: str) -> int:
        if self._alive(key) and self.values[key] == token:
            return await self.delete(key)
        return 0


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(wcs, "_redis", AsyncMock(return_value=fake))
    monkeypatch.setattr(wcs, "_LOCAL_STARTED", {})
    monkeypatch.setattr(wcs, "_LOCAL_COMPLETED", {})
    monkeypatch.setattr(wcs, "_LOCAL_BACKLOG", set())
    return fake


@pytest.mark.asyncio
async def test_a_job_never_run_is_due_at_the_first_tick_after_a_boot(redis):
    claim = await wcs.claim(JOB, now=T0)
    assert claim is not None
    await wcs.complete(claim, now=T0 + timedelta(minutes=2))
    assert await wcs.claim(JOB, now=T0 + timedelta(minutes=30)) is None
    # An hour after the last COMPLETED pass, whatever restarted in between.
    assert await wcs.claim(JOB, now=T0 + timedelta(minutes=62)) is not None


@pytest.mark.asyncio
async def test_redeploys_every_ten_minutes_never_starve_the_job(redis):
    """Each boot is a fresh process: only the stored last pass decides."""
    runs = 0
    for minute in range(0, 180, 10):
        redis.now = T0 + timedelta(minutes=minute)
        claim = await wcs.claim(JOB, now=redis.now)
        if claim is not None:
            runs += 1
            await wcs.complete(claim, now=redis.now)
    assert runs == 3  # 0, 60 and 120 minutes


@pytest.mark.asyncio
async def test_two_processes_never_run_one_pass_twice(redis):
    first = await wcs.claim(JOB, now=T0)
    second = await wcs.claim(JOB, now=T0 + timedelta(seconds=5))
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_a_pass_killed_before_its_end_is_run_again_once_its_lock_expires(redis):
    assert await wcs.claim(JOB, now=T0) is not None  # the process dies: no complete()
    redis.now = T0 + JOB.lock_ttl - timedelta(seconds=1)
    assert await wcs.claim(JOB, now=redis.now) is None
    redis.now = T0 + JOB.lock_ttl + timedelta(seconds=1)
    assert await wcs.claim(JOB, now=redis.now) is not None


@pytest.mark.asyncio
async def test_a_slow_pass_never_releases_the_lock_another_process_took_over(redis):
    slow = await wcs.claim(JOB, now=T0)
    redis.now = T0 + JOB.lock_ttl + timedelta(seconds=1)
    takeover = await wcs.claim(JOB, now=redis.now)
    assert slow is not None and takeover is not None
    await wcs.complete(slow, now=redis.now)
    assert await redis.get(wcs._key(JOB, "lock")) == takeover.token


@pytest.mark.asyncio
async def test_the_status_reads_the_persisted_pass_never_a_clock(redis):
    claim = await wcs.claim(JOB, now=T0)
    await wcs.complete(claim, now=T0 + timedelta(minutes=3))
    state = await wcs.status(JOB, now=T0 + timedelta(minutes=10))
    assert state.last_completed_at == T0 + timedelta(minutes=3)
    assert state.next_due_at == T0 + timedelta(minutes=63)
    assert not state.overdue
    later = await wcs.status(JOB, now=T0 + timedelta(minutes=70))
    assert later.next_due_at is None  # due now: it runs at the next tick
    assert not later.overdue


@pytest.mark.asyncio
async def test_no_pass_in_two_intervals_is_overdue(redis):
    await redis.set(wcs._key(JOB, "since"), T0.isoformat())
    assert not (await wcs.status(JOB, now=T0 + timedelta(minutes=110))).overdue
    assert (await wcs.status(JOB, now=T0 + timedelta(minutes=121))).overdue


@pytest.mark.asyncio
async def test_without_redis_a_process_runs_a_job_once_per_interval(monkeypatch):
    monkeypatch.setattr(wcs, "_redis", AsyncMock(side_effect=ConnectionError("down")))
    monkeypatch.setattr(wcs, "_LOCAL_STARTED", {})
    monkeypatch.setattr(wcs, "_LOCAL_COMPLETED", {})
    assert await wcs.claim(JOB, now=T0) is not None
    assert await wcs.claim(JOB, now=T0 + timedelta(minutes=1)) is None
    assert await wcs.claim(JOB, now=T0 + timedelta(minutes=61)) is not None


@pytest.mark.asyncio
async def test_the_backlog_flag_drains_on_every_tick_until_cleared(redis):
    drain = wcs.RETENTION_DRAIN
    await wcs.set_backlog(drain, True)
    assert await wcs.backlog_pending(drain)
    first = await wcs.claim(drain, now=T0)
    await wcs.complete(first, now=T0 + timedelta(seconds=20))
    # Interval 0: the very next tick runs another pass.
    assert await wcs.claim(drain, now=T0 + timedelta(seconds=60)) is not None
    await wcs.set_backlog(drain, False)
    assert not await wcs.backlog_pending(drain)


# -- the tick ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retention_pass_flags_a_backlog_and_clears_it_once_drained(monkeypatch, redis):
    from app.services import autonomous

    rounds = iter([(2000, True), (300, False)])
    monkeypatch.setattr(autonomous, "_cleanup_check_files", AsyncMock(side_effect=lambda _db: next(rounds)))
    monkeypatch.setattr(autonomous, "_cleanup_retained_files", AsyncMock(return_value=(0, False)))
    db = SimpleNamespace(rollback=AsyncMock())

    await autonomous._run_retention_pass(db)
    assert await wcs.backlog_pending(wcs.RETENTION_DRAIN)
    await autonomous._run_retention_pass(db)
    assert not await wcs.backlog_pending(wcs.RETENTION_DRAIN)


@pytest.mark.asyncio
async def test_a_failing_check_file_sweep_never_starves_the_file_retention(monkeypatch, redis):
    from app.services import autonomous

    monkeypatch.setattr(autonomous, "_cleanup_check_files", AsyncMock(side_effect=RuntimeError("boom")))
    retained = AsyncMock(return_value=(4, True))
    monkeypatch.setattr(autonomous, "_cleanup_retained_files", retained)
    db = SimpleNamespace(rollback=AsyncMock())

    await autonomous._run_retention_pass(db)
    retained.assert_awaited_once()
    assert await wcs.backlog_pending(wcs.RETENTION_DRAIN)


@pytest.mark.asyncio
async def test_the_check_file_pass_says_more_only_when_it_stopped_at_its_cap(monkeypatch):
    from app.services import check_files

    batches = iter([500, 500, 500, 500])
    monkeypatch.setattr(check_files, "sweep", AsyncMock(side_effect=lambda _db, **_kw: next(batches)))
    assert await check_files.sweep_pass(object(), limit=2000) == (2000, True)

    batches = iter([500, 120, 0])
    monkeypatch.setattr(check_files, "sweep", AsyncMock(side_effect=lambda _db, **_kw: next(batches)))
    assert await check_files.sweep_pass(object(), limit=2000) == (620, False)


def test_no_hourly_job_counts_ticks_in_memory_any_more():
    """A per-process tick counter of 60 never fires on a platform that redeploys hourly or faster."""
    import inspect

    from app.services import autonomous

    for name in (
        "_tick_counter",
        "_skill_tick_counter",
        "_ai_metrics_tick_counter",
        "_model_prices_git_sync_tick_counter",
    ):
        assert not hasattr(autonomous, name), name
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    for job in ("HOURLY_MAINTENANCE", "SKILL_REFRESH", "AI_METRICS_SYNC", "model_prices_git_sync("):
        assert f"wall_clock_schedule.{job}" in source, job


# -- Settings and Platform Health ----------------------------------------------------


@pytest.mark.asyncio
async def test_settings_reads_the_next_sweep_from_the_persisted_schedule(monkeypatch, redis):
    from app.api.v1 import file_retention as api
    from app.services import file_retention

    monkeypatch.setattr(file_retention, "load_policy", AsyncMock(return_value={}))
    monkeypatch.setattr(file_retention, "CATEGORIES", ())
    monkeypatch.setattr(file_retention, "holdings", AsyncMock(return_value={}))
    monkeypatch.setattr(file_retention, "last_sweep", AsyncMock(return_value=None))
    claim = await wcs.claim(JOB, now=datetime.now(UTC) - timedelta(minutes=20))
    await wcs.complete(claim, now=datetime.now(UTC) - timedelta(minutes=19))

    body = await api._payload(SimpleNamespace(), SimpleNamespace(is_admin=True))
    completed = datetime.fromisoformat(body["maintenance_completed_at"])
    assert datetime.fromisoformat(body["next_sweep_at"]) == completed + timedelta(hours=1)
    assert body["sweep_overdue"] is False


@pytest.mark.asyncio
async def test_the_health_check_fires_when_the_maintenance_is_overdue(redis):
    from app.services.health import platform_checks

    now = T0 + timedelta(hours=3)
    assert await platform_checks.maintenance_stalled(SimpleNamespace(), now) == []  # never scheduled
    await redis.set(wcs._key(JOB, "since"), T0.isoformat())
    [stalled] = await platform_checks.maintenance_stalled(SimpleNamespace(), now)
    assert stalled.failing
    assert "has never completed" in stalled.recommendation
    # Never completed: no "never" word as a value, overdue two intervals after the first schedule.
    assert stalled.evidence["values"] == {"overdue_since": (T0 + 2 * JOB.interval).isoformat()}

    claim = await wcs.claim(JOB, now=now)
    await wcs.complete(claim, now=now)
    [fine] = await platform_checks.maintenance_stalled(SimpleNamespace(), now + timedelta(minutes=5))
    assert not fine.failing
    assert "overdue_since" not in fine.evidence["values"]

    # Completed 3 hours ago, first scheduled long before: overdue since last completed + 2 intervals,
    # never since the first schedule (UX round 3 B1 on #769).
    later = now + timedelta(hours=3)
    [late] = await platform_checks.maintenance_stalled(SimpleNamespace(), later)
    assert late.failing
    assert late.evidence["values"] == {
        "last_completed_at": now.isoformat(),
        "overdue_since": (now + 2 * JOB.interval).isoformat(),
    }
    assert f"has not run since {now.isoformat()}" in late.recommendation
