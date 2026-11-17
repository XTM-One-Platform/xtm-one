"""The AI metrics collector: the rollback gate and the platform-only orchestrator.

The development collectors (GitHub Copilot, Cursor) were removed in #11380:
``collect_all_metrics`` runs the platform collector alone, commits its work,
and on a failure rolls back and reports the error instead of raising.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.ai_metrics_collector as collector_mod


def _mock_db(*, active: bool):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.is_active = active
    return db


def _session(db):
    @asynccontextmanager
    async def _factory():
        yield db

    return _factory


@pytest.mark.asyncio
async def test_rollback_skipped_on_a_healthy_session():
    db = _mock_db(active=True)
    assert await collector_mod._rollback_session(db, context="test") is False
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_runs_on_a_poisoned_session():
    db = _mock_db(active=False)
    assert await collector_mod._rollback_session(db, context="test") is True
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_forced_rollback_runs_on_a_healthy_session():
    db = _mock_db(active=True)
    assert await collector_mod._rollback_session(db, context="test", force=True) is True
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_all_metrics_runs_the_platform_collector_alone():
    db = _mock_db(active=True)
    with (
        patch.object(collector_mod, "async_session", _session(db)),
        patch.object(collector_mod, "collect_platform_metrics", AsyncMock(return_value=7)) as platform,
    ):
        summary = await collector_mod.collect_all_metrics()
    assert summary == {"copilot_platform": 7}
    platform.assert_awaited_once_with(db)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_all_metrics_reports_a_failure_and_rolls_back():
    db = _mock_db(active=True)
    with (
        patch.object(collector_mod, "async_session", _session(db)),
        patch.object(
            collector_mod, "collect_platform_metrics", AsyncMock(side_effect=RuntimeError("db connection dropped"))
        ),
    ):
        summary = await collector_mod.collect_all_metrics()
    assert summary == {"copilot_platform": "error: db connection dropped"}
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


def test_no_development_collector_is_left():
    for name in (
        "collect_github_copilot_metrics",
        "collect_github_copilot_enterprise_metrics",
        "collect_cursor_metrics",
        "match_external_user",
    ):
        assert not hasattr(collector_mod, name), name
