"""Tests for the `/metrics` endpoint's `usage_types` selection
(spec 002-sandbox-quota-usage-tracking, FR-012).

No existing test file covers `app/api/v1/metrics.py`'s `usage_types` /
`_read_quota_total` logic at all (confirmed by repo search). These tests
call the `metrics()` handler function directly (bypassing the FastAPI
router/auth layer) and spy on `_read_quota_total` to observe which
`usage_type` values it's invoked with, rather than parsing the Prometheus
text exposition format.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.api.v1 import metrics as metrics_module


def _patch_common(monkeypatch, platform_mode: str) -> list[str]:
    """Patch collaborators so metrics() runs without a real DB/Redis, and
    return a list that gets one entry per _read_quota_total call, recording
    the usage_type it was invoked with."""
    monkeypatch.setattr(metrics_module.settings, "platform_mode", platform_mode)

    fake_ps = MagicMock(id="platform-id")
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        AsyncMock(return_value=fake_ps),
    )
    monkeypatch.setattr(
        metrics_module, "redis_client", AsyncMock(return_value=MagicMock())
    )

    seen_usage_types: list[str] = []

    async def fake_read_quota_total(db, redis, qt, period_key, semantic, platform_id):
        seen_usage_types.append(qt)

    monkeypatch.setattr(metrics_module, "_read_quota_total", fake_read_quota_total)
    return seen_usage_types


async def test_metrics_includes_sandbox_unconditionally_in_xtm_one_mode(monkeypatch):
    seen = _patch_common(monkeypatch, "xtm_one")

    await metrics_module.metrics(db=MagicMock())

    assert "sandbox" in seen
    assert "agentic" in seen
    # llm_proxy is excluded in xtm_one mode — unaffected by this feature
    assert "llm_proxy" not in seen


async def test_metrics_includes_sandbox_unconditionally_outside_xtm_one_mode(monkeypatch):
    seen = _patch_common(monkeypatch, "copilot")

    await metrics_module.metrics(db=MagicMock())

    assert "sandbox" in seen
    assert "agentic" in seen
    assert "llm_proxy" in seen


async def test_metrics_sandbox_appears_for_every_period(monkeypatch):
    """usage_types is looped once per period (daily/monthly/yearly) —
    sandbox must appear for all of them, not just one."""
    seen = _patch_common(monkeypatch, "xtm_one")

    await metrics_module.metrics(db=MagicMock())

    assert seen.count("sandbox") == len(metrics_module.get_current_periods())
