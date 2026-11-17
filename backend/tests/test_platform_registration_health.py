"""Unit tests for ``PlatformRegistration.is_healthy`` - the single source of
truth for the platform connectivity indicator (green/red dot).

Regression context: the CTEM command center used to read product health from the
up-to-24h-cached posture payload, so a platform whose heartbeat lapsed once
stayed red on the dashboard until the cache expired - even after it recovered
(observed with OpenAEV). Health is now computed live from the registration
(status active + recent heartbeat) and the dashboard reads it from the
(uncached, 30s-refetched) /platform/registrations endpoint. These tests lock in
the health semantics and the staleness window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.platform_registration import (
    HEARTBEAT_HEALTHY_WINDOW,
    PlatformRegistration,
)


def _reg(**overrides) -> PlatformRegistration:
    """An in-memory registration (no DB session needed for is_healthy)."""
    defaults = {
        "platform_identifier": "openaev",
        "platform_url": "https://aev.example",
        "platform_title": "OpenAEV",
        "platform_version": "2.0",
        "status": "active",
        "last_heartbeat_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return PlatformRegistration(**defaults)


def test_is_healthy_active_and_fresh():
    assert _reg(last_heartbeat_at=datetime.now(UTC)).is_healthy() is True


def test_is_healthy_just_inside_window():
    now = datetime.now(UTC)
    last = now - HEARTBEAT_HEALTHY_WINDOW + timedelta(seconds=30)
    assert _reg(last_heartbeat_at=last).is_healthy(now) is True


def test_is_healthy_stale_beyond_window():
    now = datetime.now(UTC)
    last = now - HEARTBEAT_HEALTHY_WINDOW - timedelta(seconds=30)
    assert _reg(last_heartbeat_at=last).is_healthy(now) is False


def test_is_healthy_false_when_not_active():
    now = datetime.now(UTC)
    assert _reg(status="inactive", last_heartbeat_at=now).is_healthy(now) is False


def test_is_healthy_false_when_no_heartbeat():
    assert _reg(last_heartbeat_at=None).is_healthy() is False


def test_is_healthy_tolerates_naive_heartbeat():
    # A naive timestamp is interpreted as UTC rather than raising on the
    # tz-aware/naive subtraction.
    now = datetime.now(UTC)
    naive = now.replace(tzinfo=None)
    assert _reg(last_heartbeat_at=naive).is_healthy(now) is True


def test_window_tolerates_reregistration_cadence():
    # The window must comfortably exceed the ~5-min re-registration cadence (the
    # only thing that refreshes last_heartbeat_at - there is no /heartbeat route),
    # so a single delayed heartbeat never flips the indicator red. This is the
    # regression that left OpenAEV stuck red on the dashboard.
    assert timedelta(minutes=10) <= HEARTBEAT_HEALTHY_WINDOW
    now = datetime.now(UTC)
    six_minutes_old = now - timedelta(minutes=6)
    assert _reg(last_heartbeat_at=six_minutes_old).is_healthy(now) is True
