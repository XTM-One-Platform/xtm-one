"""The CTEM command center refreshes on landing and says so (#12183).

Regression of the command center v2: a stale posture was rebuilt in the
background without the page knowing, and a ready briefing of any age was never
regenerated on landing. These tests pin the server half of the contract:

* the posture read answers ``refreshing`` while a stale copy is rebuilt;
* the briefing is ``stale`` past ``BRIEFING_FRESH_HOURS``;
* an automatic regeneration is quota exempt only when the server itself sees
  the stored artifact stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1 import ctem_metrics


def test_a_briefing_past_the_daily_window_is_stale():
    now = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    window = timedelta(hours=ctem_metrics.BRIEFING_FRESH_HOURS)
    assert ctem_metrics._briefing_is_stale(now - window - timedelta(minutes=1), now) is True
    assert ctem_metrics._briefing_is_stale(now - window + timedelta(minutes=1), now) is False
    assert ctem_metrics._briefing_is_stale(None, now) is False


def test_a_naive_briefing_timestamp_is_read_as_utc():
    now = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    naive = (now - timedelta(days=15)).replace(tzinfo=None)
    assert ctem_metrics._briefing_is_stale(naive, now) is True


def test_the_briefing_window_is_a_day():
    assert ctem_metrics.BRIEFING_FRESH_HOURS == 24


def test_a_stale_posture_reads_as_refreshing(monkeypatch):
    monkeypatch.setattr(ctem_metrics, "_bg_refresh_inflight", set())
    assert ctem_metrics._posture_refreshing("k", stale=True) is True
    assert ctem_metrics._posture_refreshing("k", stale=False) is False


def test_a_rebuild_in_flight_reads_as_refreshing(monkeypatch):
    monkeypatch.setattr(ctem_metrics, "_bg_refresh_inflight", {"k"})
    assert ctem_metrics._posture_refreshing("k", stale=False) is True


@pytest.mark.parametrize(
    ("artifact", "age", "expected"),
    [
        ({"headline": "h"}, timedelta(days=15), True),
        ({"headline": "h"}, timedelta(hours=1), False),
        (None, timedelta(days=15), False),
    ],
)
async def test_the_quota_exemption_reads_the_stored_artifact(monkeypatch, artifact, age, expected):
    created = datetime.now(UTC) - age

    async def _latest(_db, _agent_id, _user_id, _vertical):
        return SimpleNamespace(created_at=created), artifact

    monkeypatch.setattr(ctem_metrics, "_latest_vertical_artifact", _latest)
    stale = await ctem_metrics._latest_artifact_is_stale(None, "a", "u", "cyber", ctem_metrics._briefing_is_stale)
    assert stale is expected


def test_both_refresh_routes_exempt_an_automatic_stale_regeneration_only():
    import inspect

    for route in (ctem_metrics.refresh_my_briefing, ctem_metrics.refresh_stage_assessment):
        source = inspect.getsource(route)
        assert "auto: bool = Query(default=False)" in source
        assert "elif auto and await _latest_artifact_is_stale(" in source
