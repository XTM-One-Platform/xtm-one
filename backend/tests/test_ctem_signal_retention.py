# SPDX-License-Identifier: Apache-2.0

"""#2581 and #2726 without a database: the windows, the purge guard and the pass.

The SQL rules run on PostgreSQL in ``test_ctem_signal_retention_postgres.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.services.coordination_retention as cr
from app.core.config import Settings
from app.services import ctem_signal_purge as purge
from app.services import ctem_signal_retention as retention


def test_the_defaults_are_30_and_90_days():
    assert Settings.model_fields["ctem_signal_discarded_retention_days"].default == 30
    assert Settings.model_fields["ctem_signal_watchlist_retention_days"].default == 90
    assert Settings.model_fields["ctem_signal_unmatched_retention_days"].default == 30


def test_unmatched_signals_have_their_own_window_and_keep_a_dismissal():
    assert retention.STATE_WINDOWS["to_validate"] == "ctem_signal_unmatched_days"
    from datetime import UTC, datetime

    rules = retention.expired_rules("to_validate", datetime(2026, 11, 28, tzinfo=UTC))
    sql = " ".join(str(rule) for rule in rules)
    assert "ctem_signals.decision[" in sql, "a signal a person dismissed is never deleted"
    assert len(rules) == len(retention.expired_rules("discarded", datetime(2026, 11, 28, tzinfo=UTC)))


def test_the_admin_window_wins_and_0_turns_a_rule_off(monkeypatch):
    monkeypatch.setattr(cr.settings, "ctem_signal_discarded_retention_days", 30)
    monkeypatch.setattr(cr.settings, "ctem_signal_watchlist_retention_days", 90)
    windows = cr.coordination_retention_days({"ctem_signal_discarded_days": 0, "ctem_signal_watchlist_days": 45})
    assert windows["ctem_signal_discarded_days"] == 0
    assert windows["ctem_signal_watchlist_days"] == 45


def test_a_settings_body_without_the_signal_windows_stays_valid():
    from app.api.v1.settings import CoordinationRetention

    body = CoordinationRetention(
        work_item_resolved_days=90,
        work_item_abandoned_days=14,
        shared_state_resolved_days=90,
        shared_state_archive_days=14,
        workset_resolved_days=7,
    )
    assert body.ctem_signal_discarded_days is None and body.ctem_signal_watchlist_days is None
    assert body.ctem_signal_unmatched_days is None
    with pytest.raises(ValueError):
        CoordinationRetention(**{**body.model_dump(), "ctem_signal_unmatched_days": -1})
    with pytest.raises(ValueError):
        CoordinationRetention(**{**body.model_dump(), "ctem_signal_watchlist_days": 3651})


@pytest.mark.asyncio
async def test_0_windows_open_no_session():
    def _no_session():
        raise AssertionError("a 0 window opens no session")

    deleted, more = await retention.purge_expired_signals(
        {"ctem_signal_discarded_days": 0, "ctem_signal_watchlist_days": 0, "ctem_signal_unmatched_days": 0},
        sessions=_no_session,
    )
    assert deleted == {"discarded": 0, "watchlist": 0, "to_validate": 0} and more is False


class _Redis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = keys

    async def exists(self, key: str) -> int:
        return int(key in self.keys)


@pytest.mark.asyncio
async def test_a_running_purge_is_seen_through_the_pause_the_lock_or_a_local_job(monkeypatch):
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    monkeypatch.setattr(purge, "_JOBS", set())
    store: set[str] = set()

    async def _redis() -> Any:
        return _Redis(store)

    monkeypatch.setattr(purge, "_redis", _redis)
    assert await retention.purge_running() is False
    store.add(purge.JOB_LOCK_KEY)
    assert await retention.purge_running() is True
    store.clear()
    store.add(purge.PAUSE_KEY)
    assert await retention.purge_running() is True
    store.clear()
    job = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(purge, "_JOBS", {job})
    assert await retention.purge_running() is True
    job.set_result(None)


@pytest.mark.asyncio
async def test_a_redis_outage_never_stops_retention(monkeypatch):
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    monkeypatch.setattr(purge, "_JOBS", set())

    async def _down() -> Any:
        raise ConnectionError("no redis")

    monkeypatch.setattr(purge, "_redis", _down)
    assert await retention.purge_running() is False


@pytest.mark.asyncio
async def test_the_pass_is_skipped_during_a_purge(monkeypatch):
    monkeypatch.setattr(retention, "purge_running", AsyncMock(return_value=True))
    sweep = AsyncMock()
    monkeypatch.setattr(retention, "purge_expired_signals", sweep)
    deleted, more = await retention.run_pass()
    assert deleted == {"discarded": 0, "watchlist": 0, "to_validate": 0} and more is False
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_pass_audits_only_when_it_deleted(monkeypatch):
    monkeypatch.setattr(retention, "purge_running", AsyncMock(return_value=False))
    monkeypatch.setattr(cr, "effective_coordination_retention", AsyncMock(return_value={"x": 1}))
    audit = AsyncMock()
    monkeypatch.setattr(retention, "audit_signal_retention_pass", audit)
    monkeypatch.setattr(
        retention, "purge_expired_signals", AsyncMock(return_value=({"discarded": 0, "watchlist": 0}, False))
    )
    await retention.run_pass()
    audit.assert_not_awaited()
    monkeypatch.setattr(
        retention, "purge_expired_signals", AsyncMock(return_value=({"discarded": 3, "watchlist": 1}, True))
    )
    deleted, more = await retention.run_pass()
    assert deleted == {"discarded": 3, "watchlist": 1} and more is True
    audit.assert_awaited_once_with({"discarded": 3, "watchlist": 1}, {"x": 1})
