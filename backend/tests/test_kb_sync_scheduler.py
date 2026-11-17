"""Regression tests for KB sync scheduler timeout configuration."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import kb_sync_scheduler


class _SourcesResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeNested:
    """Stand-in for the AsyncSessionTransaction returned by begin_nested()."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_kb_sync_timeout_secs_is_clamped(monkeypatch):
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 0)
    assert kb_sync_scheduler._kb_sync_timeout_secs() == 1

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 900)
    assert kb_sync_scheduler._kb_sync_timeout_secs() == 900

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1_000_000)
    assert kb_sync_scheduler._kb_sync_timeout_secs() == 86_400


def test_settings_model_validator_clamps_kb_sync_timeout():
    """The clamp also runs at ``Settings`` parse time via the
    ``_apply_mode_defaults`` model_validator, so a misconfigured env var
    (0, negative, or >24h) is normalised at startup — not only by the
    runtime helper in :mod:`kb_sync_scheduler`. This pins the documented
    "(capped at 86400)" contract on the model itself."""
    from app.core.config import Settings

    too_small = Settings(kb_sync_timeout_secs=0)
    assert too_small.kb_sync_timeout_secs == 1

    negative = Settings(kb_sync_timeout_secs=-42)
    assert negative.kb_sync_timeout_secs == 1

    valid = Settings(kb_sync_timeout_secs=900)
    assert valid.kb_sync_timeout_secs == 900

    too_large = Settings(kb_sync_timeout_secs=1_000_000)
    assert too_large.kb_sync_timeout_secs == 86_400

    # Default value (1800) survives validation unchanged — happy path.
    # The per-attempt execution timeout is intentionally kept generous so
    # large repo/drive syncs aren't aborted; stale detection is handled by
    # the decoupled ``kb_sync_stale_after_secs`` window instead.
    default = Settings()
    assert default.kb_sync_timeout_secs == 1800


def test_settings_clamps_stale_window_and_heartbeat():
    """The stale-detection window is decoupled from the per-sync timeout
    and clamped to ``[60, 86_400]``; the heartbeat cadence is kept
    strictly below the window so a healthy sync beats several times before
    it could be judged stale."""
    from app.core.config import Settings

    # Defaults: 10 min window, 60s heartbeat.
    default = Settings()
    assert default.kb_sync_stale_after_secs == 600
    assert default.kb_sync_heartbeat_interval_secs == 60

    # Too-small / negative window floors at 60s.
    assert Settings(kb_sync_stale_after_secs=0).kb_sync_stale_after_secs == 60
    assert Settings(kb_sync_stale_after_secs=-5).kb_sync_stale_after_secs == 60

    # Too-large window caps at 24h.
    assert Settings(kb_sync_stale_after_secs=10_000_000).kb_sync_stale_after_secs == 86_400

    # A heartbeat interval >= the window is pulled below it (window // 2).
    tight = Settings(kb_sync_stale_after_secs=120, kb_sync_heartbeat_interval_secs=999)
    assert tight.kb_sync_heartbeat_interval_secs <= 60

    # A misconfigured 0/negative heartbeat floors at 5s.
    assert Settings(kb_sync_heartbeat_interval_secs=0).kb_sync_heartbeat_interval_secs == 5


def test_settings_clamps_model_prices_git_sync_bounds():
    """Model-prices git sync settings are bounded at parse time.

    Interval is capped to [1, 10_080] minutes (1 minute to 7 days), timeout
    to [5, 600] seconds.
    """
    from app.core.config import Settings

    low = Settings(
        model_prices_git_sync_interval_minutes=0,
        model_prices_git_sync_timeout_secs=0,
    )
    assert low.model_prices_git_sync_interval_minutes == 1
    assert low.model_prices_git_sync_timeout_secs == 5

    high = Settings(
        model_prices_git_sync_interval_minutes=999_999,
        model_prices_git_sync_timeout_secs=999_999,
    )
    assert high.model_prices_git_sync_interval_minutes == 10_080
    assert high.model_prices_git_sync_timeout_secs == 600

    ok = Settings(
        model_prices_git_sync_interval_minutes=120,
        model_prices_git_sync_timeout_secs=45,
    )
    assert ok.model_prices_git_sync_interval_minutes == 120
    assert ok.model_prices_git_sync_timeout_secs == 45


@pytest.mark.asyncio
async def test_execute_sync_source_uses_timeout_for_statement_timeout(monkeypatch):
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github_public",
        config={"repo": "XTM-One-Platform/xtm-one"},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    run_sync = AsyncMock(return_value=(["doc-1"], 0))
    log_activity = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", run_sync)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)

    doc_ids = await kb_sync_scheduler.execute_sync_source(
        db,
        source,
        kb,
        "system",
        timeout_secs=1200,
    )

    assert doc_ids == ["doc-1"]
    assert run_sync.await_args.kwargs["statement_timeout_secs"] == 1200


@pytest.mark.asyncio
async def test_process_scheduled_kb_syncs_uses_configured_timeout(monkeypatch):
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=None,
    )
    current_source = SimpleNamespace(
        id=source_id, source_type="github_public", last_sync_status=None
    )
    kb = SimpleNamespace(id=kb_id, name="KB", user_id=None)
    timed_out_source = SimpleNamespace(
        id=source_id,
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        last_sync_documents=None,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                _ScalarResult(current_source),
                _ScalarResult(kb),
                _ScalarResult(timed_out_source),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    observed_timeouts: list[int] = []

    async def _fake_wait_for(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1200)
    monkeypatch.setattr(kb_sync_scheduler.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", AsyncMock())

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 1
    assert observed_timeouts == [1200]
    assert timed_out_source.last_sync_error == "Sync timed out after 1200s"


@pytest.mark.asyncio
async def test_process_scheduled_kb_syncs_prefers_source_timeout_override(monkeypatch):
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=None,
    )
    current_source = SimpleNamespace(
        id=source_id,
        source_type="github_public",
        last_sync_status=None,
        config={"timeout_secs": 2400},
    )
    kb = SimpleNamespace(id=kb_id, name="KB", user_id=None)
    timed_out_source = SimpleNamespace(
        id=source_id,
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        last_sync_documents=None,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                _ScalarResult(current_source),
                _ScalarResult(kb),
                _ScalarResult(timed_out_source),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    observed_timeouts: list[int] = []

    async def _fake_wait_for(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1200)
    monkeypatch.setattr(kb_sync_scheduler.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", AsyncMock())

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 1
    assert observed_timeouts == [2400]
    assert timed_out_source.last_sync_error == "Sync timed out after 2400s"


def test_source_timeout_secs_override_parsing():
    """Direct unit coverage of ``_source_timeout_secs`` value handling."""

    def src(config):
        return SimpleNamespace(config=config)

    default = 1800

    # No config / no key / None value -> global default.
    assert kb_sync_scheduler._source_timeout_secs(src(None), default_timeout_secs=default) == default
    assert kb_sync_scheduler._source_timeout_secs(src({}), default_timeout_secs=default) == default
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": None}), default_timeout_secs=default) == default
    )

    # Valid int and numeric string (UI config values are strings) are honored.
    assert kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": 7200}), default_timeout_secs=default) == 7200
    assert kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": "7200"}), default_timeout_secs=default) == 7200

    # Out-of-range values are clamped to [1, 86_400].
    assert kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": 0}), default_timeout_secs=default) == 1
    assert kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": -5}), default_timeout_secs=default) == 1
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": 1_000_000}), default_timeout_secs=default)
        == 86_400
    )

    # Invalid values fall back to the default instead of raising.
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": "nope"}), default_timeout_secs=default) == default
    )
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": [3600]}), default_timeout_secs=default) == default
    )
    # Booleans must be rejected: int(True) == 1 would otherwise clamp the
    # sync to a 1-second timeout.
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": True}), default_timeout_secs=default) == default
    )
    # Floats must be rejected too: the contract is ints and numeric strings
    # only, and int(1.5) would otherwise silently truncate to 1.
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": 1.5}), default_timeout_secs=default) == default
    )
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": 2400.0}), default_timeout_secs=default) == default
    )
    assert (
        kb_sync_scheduler._source_timeout_secs(src({"timeout_secs": "7200.5"}), default_timeout_secs=default)
        == default
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_timeout"),
    [
        # Override present -> the per-source budget wins over the global.
        ({"timeout_secs": 2400}, 2400),
        # No override -> the global default applies (sources without
        # ``config.timeout_secs`` are completely unaffected by the feature).
        ({"repo": "org/repo"}, 1200),
    ],
)
async def test_execute_sync_source_manual_path_honors_source_timeout(
    monkeypatch, config, expected_timeout
):
    """When the caller passes no ``timeout_secs`` (manual "Sync now" /
    "Force re-sync" background path), ``execute_sync_source`` must fall back
    to the per-source ``config.timeout_secs`` override — not the raw global
    default — so a source tuned for a longer budget keeps it on manual
    syncs too. Sources without an override keep the global default."""
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github_public",
        config=config,
        sync_failure_count=0,
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        sync_heartbeat_at=None,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    db = SimpleNamespace(
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    observed: dict[str, int] = {}

    async def _fake_isolated_run(*, statement_timeout_secs, **_kw):
        observed["statement_timeout_secs"] = statement_timeout_secs
        return [], 0

    async def _noop_activity(*_a, **_kw):
        return None

    async def _noop_heartbeat(_source_id):
        return None

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1200)
    monkeypatch.setattr(
        kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_isolated_run
    )
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    import app.services.kb_helpers as kb_helpers

    monkeypatch.setattr(kb_helpers, "log_kb_activity", _noop_activity)

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "user-1")

    assert observed["statement_timeout_secs"] == expected_timeout


def test_stale_window_is_decoupled_from_per_sync_timeout(monkeypatch):
    """The stale-detection window tracks
    ``kb_sync_stale_after_secs`` and is INDEPENDENT of the (possibly
    large) per-attempt execution timeout — so a generous timeout for big
    repos no longer forces a ~35 min stuck window."""
    # A large per-sync timeout must NOT widen the stale window.
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1800)
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    assert kb_sync_scheduler._stale_sync_window() == timedelta(minutes=10)

    # Tuning the window changes detection without touching the timeout.
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 300)
    assert kb_sync_scheduler._stale_sync_window() == timedelta(minutes=5)

    # Runtime clamp guards monkeypatched out-of-range values.
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 0)
    assert kb_sync_scheduler._stale_sync_window() == timedelta(seconds=60)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 10_000_000
    )
    assert kb_sync_scheduler._stale_sync_window() == timedelta(seconds=86_400)


# ── Auto-retry with exponential backoff ──────────────────


def _set_retry_settings(monkeypatch, *, base=600, max_delay=86_400, jitter=0,
                        alert=5):
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_retry_base_delay_seconds", base
    )
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_retry_max_delay_seconds", max_delay
    )
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_retry_jitter_seconds", jitter
    )
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_retry_alert_threshold", alert
    )


def test_sync_retry_delay_is_exponential_and_capped(monkeypatch):
    """Backoff doubles per consecutive failure (base * 2^(count-1)) and is
    clamped to ``max_delay`` — so a persistently-failing source backs off
    instead of being retried every interval."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)

    # count is the post-increment failure count: 1 → base, 2 → 2*base, ...
    assert kb_sync_scheduler._sync_retry_delay_seconds(1) == 600
    assert kb_sync_scheduler._sync_retry_delay_seconds(2) == 1200
    assert kb_sync_scheduler._sync_retry_delay_seconds(3) == 2400
    assert kb_sync_scheduler._sync_retry_delay_seconds(4) == 4800
    # Eventually clamps to the 24h cap rather than growing unbounded.
    assert kb_sync_scheduler._sync_retry_delay_seconds(20) == 86_400
    # Huge counts must not overflow — the exponent is clamped to 32.
    assert kb_sync_scheduler._sync_retry_delay_seconds(10_000) == 86_400


def test_sync_retry_delay_adds_jitter_within_bounds(monkeypatch):
    """Jitter (0..N) de-correlates a batch of failures so they don't all
    retry on the same tick; it never reduces the base delay."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=60)
    for _ in range(20):
        delay = kb_sync_scheduler._sync_retry_delay_seconds(1)
        assert 600 <= delay <= 660


def test_record_sync_failure_sets_backoff_and_status(monkeypatch):
    """``_record_sync_failure`` increments the counter, marks the source
    ``error``, and schedules ``sync_next_retry_at`` at now + backoff."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    now = datetime.now(UTC)
    source = SimpleNamespace(
        last_synced_at=None,
        last_sync_status="syncing",
        last_sync_error=None,
        last_sync_documents=5,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )

    kb_sync_scheduler._record_sync_failure(
        source,
        prior_failure_count=0,
        error_message="boom",
        now=now,
        source_id="src-1",
        source_type="website",
    )

    assert source.last_sync_status == "error"
    assert source.last_sync_error == "boom"
    assert source.last_sync_documents == 0
    assert source.sync_failure_count == 1
    assert source.sync_next_retry_at == now + timedelta(seconds=600)


def test_record_sync_failure_escalates_after_threshold(monkeypatch, caplog):
    """Once the consecutive-failure count reaches the alert threshold, the
    scheduler logs an error-level 'persistent failure' line so operators
    can alert on a source that repeatedly fails to sync."""
    import logging

    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    now = datetime.now(UTC)
    source = SimpleNamespace(
        last_synced_at=None,
        last_sync_status="syncing",
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=4,
        sync_next_retry_at=None,
    )

    with caplog.at_level(logging.ERROR):
        kb_sync_scheduler._record_sync_failure(
            source,
            prior_failure_count=4,  # 5th consecutive failure → at threshold
            error_message="still broken",
            now=now,
            source_id="src-1",
            source_type="website",
        )

    assert source.sync_failure_count == 5
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("failed 5 consecutive times" in m for m in errors), errors


def test_record_sync_failure_no_escalation_below_threshold(monkeypatch, caplog):
    """Below the threshold there must be NO error-level escalation — only
    the routine per-failure handling (avoids alert noise on a transient
    blip)."""
    import logging

    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    source = SimpleNamespace(
        last_synced_at=None,
        last_sync_status="syncing",
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )

    with caplog.at_level(logging.ERROR):
        kb_sync_scheduler._record_sync_failure(
            source,
            prior_failure_count=0,
            error_message="transient",
            now=datetime.now(UTC),
            source_id="src-1",
            source_type="website",
        )

    assert not [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "consecutive" in r.getMessage()
    ]


def test_record_sync_failure_escalates_only_once_on_crossing(monkeypatch, caplog):
    """A source ALREADY at/above the threshold must NOT re-emit the
    error-level alert on every subsequent failure — escalation fires only
    on the below→at/above transition, so a persistently-failing source
    doesn't spam an error log every backoff retry."""
    import logging

    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    source = SimpleNamespace(
        last_synced_at=None,
        last_sync_status="error",
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=5,
        sync_next_retry_at=None,
    )

    with caplog.at_level(logging.ERROR):
        kb_sync_scheduler._record_sync_failure(
            source,
            prior_failure_count=5,  # already past threshold → 6th failure
            error_message="still broken",
            now=datetime.now(UTC),
            source_id="src-1",
            source_type="website",
        )

    assert source.sync_failure_count == 6
    assert not [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "consecutive" in r.getMessage()
    ], "must not re-escalate once already above the threshold"


@pytest.mark.asyncio
async def test_execute_sync_source_resets_backoff_on_success(monkeypatch):
    """A successful sync clears the failure counter and the scheduled
    retry so the source returns to normal interval scheduling."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github_public",
        config={"repo": "XTM-One-Platform/xtm-one"},
        integration_id=None,
        last_sync_status="error",
        last_synced_at=None,
        last_sync_error="previously failed",
        last_sync_documents=0,
        sync_failure_count=3,
        sync_next_retry_at=datetime.now(UTC) + timedelta(hours=2),
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 0)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "success"
    assert source.sync_failure_count == 0
    assert source.sync_next_retry_at is None


@pytest.mark.asyncio
async def test_error_source_retries_on_backoff_not_interval(monkeypatch):
    """Core fix: an error source whose ``sync_next_retry_at`` is due MUST
    be retried even though its (recent) ``last_synced_at`` is well within
    the regular interval — otherwise a daily/weekly source stays stale for
    a day after a transient failure."""
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="daily",
        last_synced_at=now - timedelta(minutes=2),  # nowhere near 24h interval
        last_sync_status="error",
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=now - timedelta(seconds=5),  # retry is due
    )
    current_source = SimpleNamespace(
        id=source_id, source_type="github_public", last_sync_status="error"
    )
    kb = SimpleNamespace(id=kb_id, name="KB", user_id=None)

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                _ScalarResult(current_source),
                _ScalarResult(kb),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    async def _fake_wait_for(awaitable, *, timeout):
        if asyncio.iscoroutine(awaitable):
            return await awaitable
        return awaitable

    monkeypatch.setattr(kb_sync_scheduler.asyncio, "wait_for", _fake_wait_for)
    exec_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", exec_mock)

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 1
    assert exec_mock.await_count == 1


@pytest.mark.asyncio
async def test_error_source_skipped_until_backoff_due(monkeypatch):
    """Symmetry guard: an error source whose ``sync_next_retry_at`` is in
    the future MUST be skipped (still backing off) — no sync attempt, no
    re-fetch of the source/KB."""
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="daily",
        last_synced_at=now - timedelta(minutes=2),
        last_sync_status="error",
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=now + timedelta(hours=1),  # not due yet
    )

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_SourcesResult([scheduled_source])]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    exec_mock = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", exec_mock)

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 0
    assert exec_mock.await_count == 0
    # Only the initial sources query ran — the source was skipped before
    # the per-source re-fetch.
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_timeout_schedules_backoff_retry(monkeypatch):
    """A timed-out sync increments the failure counter and schedules the
    next backoff retry (not just a flat error status)."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=None,
    )
    current_source = SimpleNamespace(
        id=source_id, source_type="github_public", last_sync_status=None
    )
    kb = SimpleNamespace(id=kb_id, name="KB", user_id=None)
    timed_out_source = SimpleNamespace(
        id=source_id,
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        last_sync_documents=None,
        sync_failure_count=2,
        sync_next_retry_at=None,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                _ScalarResult(current_source),
                _ScalarResult(kb),
                _ScalarResult(timed_out_source),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    async def _fake_wait_for(awaitable, *, timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1200)
    monkeypatch.setattr(kb_sync_scheduler.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", AsyncMock())

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 1
    assert timed_out_source.last_sync_status == "error"
    assert timed_out_source.last_sync_error == "Sync timed out after 1200s"
    # 3rd consecutive failure → backoff = base * 2^2 = 2400s.
    assert timed_out_source.sync_failure_count == 3
    # ``_record_sync_failure`` stamps BOTH ``last_synced_at`` and
    # ``sync_next_retry_at`` from the same ``datetime.now(UTC)``, so the
    # backoff is exactly their difference — assert that delta (jitter 0)
    # rather than a wall-clock window against the test's ``now``, which
    # could flake on a slow/loaded CI runner.
    backoff = (
        timed_out_source.sync_next_retry_at - timed_out_source.last_synced_at
    ).total_seconds()
    assert backoff == 2400


@pytest.mark.asyncio
async def test_timeout_emits_sync_failed_activity(monkeypatch):
    """A timed-out sync must surface a user-visible ``sync_failed`` KB
    activity event. Previously the timeout path only updated the row status +
    logged server-side, so the timeout never appeared in the KB activity feed
    (the exception path logged it; the timeout path had silently drifted).
    Both paths now share ``_handle_sync_failure``."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,
        knowledge_base_id=kb_id,
        user_id=uuid.uuid4(),  # notion isn't auth-free, so it needs a user
        source_type="notion",
        sync_next_retry_at=None,
    )
    current_source = SimpleNamespace(
        id=source_id, source_type="notion", last_sync_status=None
    )
    kb = SimpleNamespace(id=kb_id, name="KB", user_id=None)
    timed_out_source = SimpleNamespace(
        id=source_id,
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        last_sync_documents=None,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                _ScalarResult(current_source),
                _ScalarResult(kb),
                _ScalarResult(timed_out_source),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        flush=AsyncMock(),
    )

    log_activity = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)

    async def _fake_wait_for(awaitable, *, timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_timeout_secs", 1800)
    monkeypatch.setattr(kb_sync_scheduler.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", AsyncMock())

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 1
    sync_failed = [
        c for c in log_activity.await_args_list if c.args[2] == "sync_failed"
    ]
    assert len(sync_failed) == 1, "timeout must emit exactly one sync_failed event"
    assert sync_failed[0].kwargs["details"] == "Sync timed out after 1800s"
    assert sync_failed[0].kwargs["source_type"] == "notion"
    # And the row is still marked error + retried with backoff.
    assert timed_out_source.last_sync_status == "error"
    assert timed_out_source.last_sync_error == "Sync timed out after 1800s"


@pytest.mark.asyncio
async def test_execute_sync_source_failure_emits_activity_and_raises(monkeypatch):
    """The per-attempt exception path records the failure, emits the
    ``sync_failed`` activity, and re-raises — now via the shared
    ``_handle_sync_failure`` helper (so it stays in lockstep with the
    timeout path)."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="notion",
        config={},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _boom(**kwargs):
        raise RuntimeError("notion exploded")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _boom)
    log_activity = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)

    with pytest.raises(RuntimeError, match="notion exploded"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "error"
    assert source.sync_failure_count == 1
    sync_failed = [
        c for c in log_activity.await_args_list if c.args[2] == "sync_failed"
    ]
    assert len(sync_failed) == 1
    assert "notion exploded" in sync_failed[0].kwargs["details"]


@pytest.mark.asyncio
async def test_handle_sync_failure_rolls_back_on_persist_error(monkeypatch):
    """If persisting the failure raises (an aborted transaction mid-flush),
    ``_handle_sync_failure`` must roll back best-effort — so the caller's
    immediate ``db.commit()`` isn't sunk by a poisoned transaction — and must
    not re-raise."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    source = SimpleNamespace(
        id=uuid.uuid4(),
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        last_sync_documents=None,
        sync_failure_count=0,
        sync_heartbeat_at=None,
        sync_next_retry_at=None,
    )

    # log_kb_activity raises → simulates the session aborting mid-persist.
    async def _boom(*_a, **_kw):
        raise RuntimeError("aborted transaction")

    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", _boom)
    rollback = AsyncMock()
    db = SimpleNamespace(flush=AsyncMock(), rollback=rollback, commit=AsyncMock())

    # Must not raise.
    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=uuid.uuid4(),
        prior_failure_count=0,
        prior_perm_fail_count=0,
        error_message="boom",
        activity_detail="boom",
        source_id=str(source.id),
        source_type="notion",
    )

    rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_secs,expected_minutes",
    [
        # 60s window = 1 min exactly.
        (60, 1),
        # 91s window = 1.52 min -> must round up to 2.
        (91, 2),
        # 300s window = 5 min exactly.
        (300, 5),
        # Default 600s window = 10 min exactly.
        (600, 10),
    ],
)
async def test_reset_stale_syncs_message_rounds_up(monkeypatch, stale_secs, expected_minutes):
    # The message now derives from the decoupled stale window, NOT the
    # per-sync timeout.
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", stale_secs)

    stale_source = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="github_public",
        last_sync_status="syncing",
        last_synced_at=datetime.now(UTC) - timedelta(days=1),
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [stale_source])

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FetchResult()),
        flush=AsyncMock(),
    )

    count = await kb_sync_scheduler.reset_stale_syncs(db)

    assert count == 1
    assert stale_source.last_sync_status == "error"
    assert f">{expected_minutes} min" in stale_source.last_sync_error
    # Sanity-check the message did not under-report the window.
    actual_window_min = stale_secs / 60
    assert expected_minutes >= actual_window_min


# ── Proportional backoff for stale resets ────────────────


def test_sync_retry_delay_override_lowers_cap(monkeypatch):
    """A stale/stuck reset can pass a lower ``max_delay_override`` so a
    transient worker restart retries within minutes instead of escalating
    to the multi-hour backoff a genuine error reaches."""
    _set_retry_settings(monkeypatch, base=300, max_delay=86_400, jitter=0)

    # Without override the 5th failure follows the full exponential.
    assert kb_sync_scheduler._sync_retry_delay_seconds(5) == 300 * 2**4  # 4800

    # With a 1800s override the same failure is capped at 30 min.
    assert (
        kb_sync_scheduler._sync_retry_delay_seconds(5, max_delay_override=1800) == 1800
    )

    # The override can only LOWER the ceiling, never raise it above the
    # hard ``max_delay`` cap.
    _set_retry_settings(monkeypatch, base=300, max_delay=600, jitter=0)
    assert (
        kb_sync_scheduler._sync_retry_delay_seconds(10, max_delay_override=86_400) == 600
    )


def test_default_retry_backoff_avoids_three_hour_jump(monkeypatch):
    """With shipped defaults, neither a genuine error nor (especially) a
    stale reset should schedule a ~3 h retry at the alert threshold."""
    # Genuine-error path: lowered base means the 5th failure tops out at
    # 80 min, not the old ~2.7 h.
    assert kb_sync_scheduler.settings.kb_sync_retry_base_delay_seconds == 300
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_retry_jitter_seconds", 0)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_retry_max_delay_seconds", 86_400
    )
    error_delay = kb_sync_scheduler._sync_retry_delay_seconds(5)
    assert error_delay == 4_800
    assert error_delay < 10_800  # < 3 h

    # Stale-reset path with the shipped 30 min cap never exceeds 1800s.
    stale_cap = kb_sync_scheduler.settings.kb_sync_stale_retry_max_delay_seconds
    assert stale_cap == 1800
    stale_delay = kb_sync_scheduler._sync_retry_delay_seconds(
        5, max_delay_override=stale_cap
    )
    assert stale_delay == 1800


@pytest.mark.asyncio
async def test_reset_stale_syncs_caps_backoff_with_stale_cap(monkeypatch):
    """``reset_stale_syncs`` applies the lower stale cap so a repeatedly
    orphaned-but-healthy source never lands in a multi-hour backoff."""
    _set_retry_settings(monkeypatch, base=300, max_delay=86_400, jitter=0)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_retry_max_delay_seconds", 1800
    )
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)

    stale_source = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="github",
        last_sync_status="syncing",
        last_synced_at=datetime.now(UTC) - timedelta(days=1),
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=4,  # reset → 5th consecutive failure
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [stale_source])

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FetchResult()),
        flush=AsyncMock(),
    )
    # Suppress the (best-effort) alert so we isolate the backoff assertion.
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock()
    )

    count = await kb_sync_scheduler.reset_stale_syncs(db)

    assert count == 1
    assert stale_source.sync_failure_count == 5
    # Uncapped this would be 300 * 2^4 = 4800s; the stale cap holds it at
    # 1800s. last_synced_at and sync_next_retry_at share the same ``now``.
    backoff = (
        stale_source.sync_next_retry_at - stale_source.last_synced_at
    ).total_seconds()
    assert backoff == 1800
    # The reset clears the now-meaningless liveness heartbeat.
    assert stale_source.sync_heartbeat_at is None


# ── Persistent-failure admin alert ───────────────────────


def test_record_sync_failure_returns_escalation_flag(monkeypatch):
    """``_record_sync_failure`` returns True exactly on the below→threshold
    crossing so callers can fire a one-shot admin alert."""
    _set_retry_settings(monkeypatch, base=300, max_delay=86_400, jitter=0, alert=5)

    def _src(count):
        return SimpleNamespace(
            last_synced_at=None,
            last_sync_status="syncing",
            last_sync_error=None,
            last_sync_documents=0,
            sync_failure_count=count,
            sync_next_retry_at=None,
            sync_heartbeat_at=datetime.now(UTC),
        )

    now = datetime.now(UTC)
    # 4 → 5 crosses the threshold → escalates.
    crossed = kb_sync_scheduler._record_sync_failure(
        _src(4), prior_failure_count=4, error_message="x", now=now,
        source_id="s", source_type="github",
    )
    assert crossed is True
    # Below threshold → no escalation.
    below = kb_sync_scheduler._record_sync_failure(
        _src(0), prior_failure_count=0, error_message="x", now=now,
        source_id="s", source_type="github",
    )
    assert below is False
    # Already above → no re-escalation.
    above = kb_sync_scheduler._record_sync_failure(
        _src(5), prior_failure_count=5, error_message="x", now=now,
        source_id="s", source_type="github",
    )
    assert above is False


@pytest.mark.asyncio
async def test_reset_stale_syncs_emits_alert_on_escalation(monkeypatch):
    """Crossing the persistent-failure threshold during a stale reset fires
    the best-effort admin alert with the source's details."""
    _set_retry_settings(monkeypatch, base=300, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)

    kb_id = uuid.uuid4()
    stale_source = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=kb_id,
        source_type="github",
        last_sync_status="syncing",
        last_synced_at=datetime.now(UTC) - timedelta(days=1),
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=4,  # → 5th, crosses threshold
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [stale_source])

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FetchResult()),
        flush=AsyncMock(),
    )
    alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", alert)

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert alert.await_count == 1
    assert alert.await_args.kwargs["kb_id"] == kb_id
    assert alert.await_args.kwargs["source_type"] == "github"
    assert alert.await_args.kwargs["failure_count"] == 5


@pytest.mark.asyncio
async def test_emit_persistent_failure_alert_is_best_effort(monkeypatch):
    """The alert helper must never raise, even if logging the activity and
    the SSE broadcast both fail — alerting cannot break a scheduler tick."""
    import app.services.kb_helpers as kb_helpers

    monkeypatch.setattr(
        kb_helpers, "log_kb_activity", AsyncMock(side_effect=RuntimeError("boom"))
    )

    async def _boom_redis():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _boom_redis)

    db = SimpleNamespace(flush=AsyncMock(), begin_nested=_FakeNested)
    # Should swallow both failures and return None.
    result = await kb_sync_scheduler._emit_persistent_failure_alert(
        db,
        kb_id=uuid.uuid4(),
        source_type="github",
        failure_count=5,
        error_message="boom",
    )
    assert result is None


# ── Liveness heartbeat ───────────────────────────────────


@pytest.mark.asyncio
async def test_execute_sync_source_runs_and_cancels_heartbeat(monkeypatch):
    """``execute_sync_source`` starts a heartbeat task for the duration of
    the sync and stamps an initial heartbeat, clearing it on success."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github_public",
        config={"repo": "XTM-One-Platform/xtm-one"},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_heartbeat(source_id):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _fake_heartbeat)

    async def _fake_sync(**kwargs):
        # Yield so the heartbeat task is scheduled before we finish.
        await asyncio.sleep(0)
        assert started.is_set()
        # Initial heartbeat was stamped before the sync began.
        assert source.sync_heartbeat_at is not None
        return ["doc-1"], 0

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_sync)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    doc_ids = await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert doc_ids == ["doc-1"]
    assert cancelled.is_set()  # heartbeat task was cancelled on completion
    assert source.last_sync_status == "success"
    # Heartbeat cleared once the sync is no longer in flight.
    assert source.sync_heartbeat_at is None


# ── Concurrent-pickup guard ──────────────────


def test_claim_source_stmt_renders_valid_postgresql():
    """The claim query must compile to valid PostgreSQL: FOR UPDATE SKIP
    LOCKED for the atomic claim, and IS DISTINCT FROM (NOT ``IS NOT``, which
    PostgreSQL only accepts with NULL/boolean operands and would raise a
    server-side syntax error — silently disabling ALL scheduled syncs since
    the loop's except block swallows the failure). Mocked-session tests
    cannot catch SQL validity, hence this compile-level guard."""
    from sqlalchemy.dialects import postgresql

    stmt = kb_sync_scheduler._claim_source_stmt(uuid.uuid4())
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "IS DISTINCT FROM 'syncing'" in sql
    assert "IS NOT 'syncing'" not in sql


@pytest.mark.asyncio
async def test_concurrent_pickup_skipped_when_row_locked(monkeypatch):
    """Regression test: when multiple worker replicas run
    the scheduler simultaneously, the second worker's FOR UPDATE SKIP LOCKED
    re-fetch returns None (row is held by the first worker) and must skip the
    source instead of launching a duplicate 1800 s sync.

    The fixture simulates the SKIP LOCKED behaviour by returning None for the
    per-source re-fetch (the second ``db.execute`` call)."""
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,  # snapshot still shows "not syncing"
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=None,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                # FOR UPDATE SKIP LOCKED: row is locked by another worker.
                _ScalarResult(None),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    exec_mock = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", exec_mock)

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 0, "source locked by another worker must not be synced"
    assert exec_mock.await_count == 0, "execute_sync_source must not be called"
    # Only two DB calls: the initial sources query + the SKIP LOCKED re-fetch.
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_pickup_skipped_when_already_syncing_on_refetch(monkeypatch):
    """Defence-in-depth guard covers the narrow
    window where another worker committed 'syncing' AFTER this tick's snapshot
    was taken but BEFORE the FOR UPDATE SKIP LOCKED re-fetch ran.

    In that case the re-fetch WHERE clause (status IS DISTINCT FROM 'syncing')
    filters the row out and returns None, preventing a duplicate sync launch."""
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    kb_id = uuid.uuid4()

    # Snapshot says "not syncing" — passes the pre-loop guard.
    scheduled_source = SimpleNamespace(
        id=source_id,
        sync_interval="6h",
        last_synced_at=now - timedelta(hours=7),
        last_sync_status=None,
        knowledge_base_id=kb_id,
        user_id=None,
        source_type="github_public",
        sync_next_retry_at=None,
    )

    # The re-fetch WHERE clause excludes rows with last_sync_status='syncing',
    # so a concurrent commit that set 'syncing' causes the query to return None.
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _SourcesResult([scheduled_source]),
                # WHERE last_sync_status IS DISTINCT FROM 'syncing' → no row
                _ScalarResult(None),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    exec_mock = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "execute_sync_source", exec_mock)

    count = await kb_sync_scheduler.process_scheduled_kb_syncs(db)

    assert count == 0, "already-syncing source must not be re-launched"
    assert exec_mock.await_count == 0
    assert db.execute.await_count == 2


# ── Stale-sync quarantine ───────────────────────────────


def _make_stale_source(**overrides):
    """Factory for a stale-sync SimpleNamespace source with sensible defaults."""
    defaults = dict(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="github",
        last_sync_status="syncing",
        last_synced_at=datetime.now(UTC) - timedelta(days=1),
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_reset_stale_syncs_increments_stale_count(monkeypatch):
    """Each stale reset increments ``sync_stale_count`` independently of
    ``sync_failure_count``."""
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 5
    )
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    source = _make_stale_source(
        sync_stale_count=2, sync_failure_count=0, sync_perm_fail_count=3
    )

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [source])

    db = SimpleNamespace(execute=AsyncMock(return_value=_FetchResult()), flush=AsyncMock())
    monkeypatch.setattr(kb_sync_scheduler, "_emit_quarantine_alert", AsyncMock())
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert source.sync_stale_count == 3
    assert source.enabled is True  # not yet at threshold
    # A hang is not a 404 — it breaks the consecutive 'not found' chain, so
    # the perm-fail budget resets.
    assert source.sync_perm_fail_count == 0


@pytest.mark.asyncio
async def test_reset_stale_syncs_quarantines_at_threshold(monkeypatch):
    """When ``sync_stale_count`` reaches the quarantine threshold the source
    is disabled and receives an explanatory error message."""
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 3
    )
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    # count=2 → third reset crosses threshold (≥ threshold after increment)
    source = _make_stale_source(sync_stale_count=2, sync_failure_count=0)

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [source])

    db = SimpleNamespace(execute=AsyncMock(return_value=_FetchResult()), flush=AsyncMock())
    monkeypatch.setattr(kb_sync_scheduler, "_emit_quarantine_alert", AsyncMock())
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert source.sync_stale_count == 3
    assert source.enabled is False
    assert source.last_sync_error is not None
    assert "quarantined" in source.last_sync_error.lower()
    # The force-resync endpoint detects quarantine by this prefix (to tell an
    # auto-disabled source apart from a manually disabled one) — pin the
    # contract so the message and the detection can never drift apart.
    assert source.last_sync_error.startswith(
        kb_sync_scheduler.QUARANTINE_ERROR_PREFIX
    )


@pytest.mark.asyncio
async def test_reset_stale_syncs_quarantines_emits_alert(monkeypatch):
    """Quarantining a source fires the best-effort ``_emit_quarantine_alert``."""
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 3
    )
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    source = _make_stale_source(
        id=source_id,
        knowledge_base_id=kb_id,
        source_type="github",
        sync_stale_count=2,
    )

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [source])

    db = SimpleNamespace(execute=AsyncMock(return_value=_FetchResult()), flush=AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert quarantine_alert.await_count == 1
    call_kwargs = quarantine_alert.await_args.kwargs
    assert call_kwargs["kb_id"] == kb_id
    assert call_kwargs["source_type"] == "github"
    assert call_kwargs["stale_count"] == 3


@pytest.mark.asyncio
async def test_reset_stale_syncs_below_threshold_no_quarantine(monkeypatch):
    """Sources below the quarantine threshold are reset normally (not disabled)."""
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 5
    )
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    source = _make_stale_source(sync_stale_count=1)

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [source])

    db = SimpleNamespace(execute=AsyncMock(return_value=_FetchResult()), flush=AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert source.sync_stale_count == 2
    assert source.enabled is True
    assert quarantine_alert.await_count == 0


@pytest.mark.asyncio
async def test_reset_stale_syncs_zero_threshold_disables_quarantine(monkeypatch):
    """Setting ``kb_sync_stale_quarantine_threshold = 0`` disables quarantine
    entirely — sources are never disabled no matter how many stale resets occur."""
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 0
    )
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    source = _make_stale_source(sync_stale_count=999)

    class _FetchResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [source])

    db = SimpleNamespace(execute=AsyncMock(return_value=_FetchResult()), flush=AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler.reset_stale_syncs(db)

    assert source.enabled is True  # never quarantined
    assert quarantine_alert.await_count == 0


@pytest.mark.asyncio
async def test_execute_sync_source_resets_stale_count_on_success(monkeypatch):
    """A successful sync resets ``sync_stale_count`` to 0 so a transient
    hang does not permanently use up the quarantine budget."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_stale_source(
        source_type="github_public",
        config={"repo": "XTM-One-Platform/xtm-one"},
        integration_id=None,
        sync_stale_count=3,
        last_sync_status=None,
        last_synced_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_heartbeat(source_id):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _fake_heartbeat)

    async def _fake_sync(**kwargs):
        await asyncio.sleep(0)
        return ["doc-1"], 0

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_sync)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.sync_stale_count == 0, "stale count must reset to 0 on success"


# ── Error-based quarantine ─────────────────────────────


def _make_error_source(**overrides):
    """Factory for a SimpleNamespace error source with sensible defaults."""
    defaults = dict(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="microsoft-drive",
        last_sync_status="error",
        last_synced_at=datetime.now(UTC) - timedelta(hours=1),
        last_sync_error="404 Not Found",
        last_sync_documents=0,
        sync_failure_count=9,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_handle_sync_failure_quarantines_at_threshold(monkeypatch):
    """When consecutive failures reach ``kb_sync_failure_quarantine_threshold``
    the source is disabled and the error message is set to the quarantine
    prefix so Force Re-sync can detect and clear it."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 10
    )
    source = _make_error_source(sync_failure_count=9)  # 10th failure triggers quarantine
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=9,
        prior_perm_fail_count=0,
        error_message="404 Not Found",
        activity_detail="Sync failed: 404 Not Found",
        source_id=str(source.id),
        source_type=source.source_type,
    )

    assert source.enabled is False, "source must be disabled at quarantine threshold"
    assert (source.last_sync_error or "").startswith(kb_sync_scheduler.QUARANTINE_ERROR_PREFIX)
    # The quarantine message replaces ``last_sync_error`` (the field the UI
    # surfaces) — the underlying root cause must survive in it so the
    # operator can fix the configuration.
    assert "404 Not Found" in source.last_sync_error
    quarantine_alert.assert_awaited_once()
    call_kwargs = quarantine_alert.await_args.kwargs
    assert call_kwargs["failure_count"] == 10
    assert call_kwargs["source_type"] == "microsoft-drive"


@pytest.mark.asyncio
async def test_handle_sync_failure_no_quarantine_below_threshold(monkeypatch):
    """Sources below the failure quarantine threshold are NOT disabled."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 10
    )
    source = _make_error_source(sync_failure_count=8)  # 9th failure — one short
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=8,
        prior_perm_fail_count=0,
        error_message="404 Not Found",
        activity_detail="Sync failed: 404 Not Found",
        source_id=str(source.id),
        source_type=source.source_type,
    )

    assert source.enabled is True, "source must remain enabled below threshold"
    quarantine_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_sync_failure_zero_threshold_disables_error_quarantine(monkeypatch):
    """Setting ``kb_sync_failure_quarantine_threshold = 0`` disables error-based
    quarantine entirely — sources failing repeatedly are never auto-disabled."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 0
    )
    source = _make_error_source(sync_failure_count=99)  # far above any threshold
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=99,
        prior_perm_fail_count=0,
        error_message="404 Not Found",
        activity_detail="Sync failed: 404 Not Found",
        source_id=str(source.id),
        source_type=source.source_type,
    )

    assert source.enabled is True
    quarantine_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_sync_failure_quarantine_emits_alert_with_correct_args(monkeypatch):
    """Quarantine triggers ``_emit_failure_quarantine_alert`` with the right
    kb_id, source_type, failure_count, and error_message."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 3
    )
    kb_id = uuid.uuid4()
    source = _make_error_source(
        knowledge_base_id=kb_id,
        source_type="github",
        sync_failure_count=2,
    )
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=kb_id,
        prior_failure_count=2,
        prior_perm_fail_count=0,
        error_message="repo not found",
        activity_detail="Sync failed: repo not found",
        source_id=str(source.id),
        source_type="github",
    )

    assert source.enabled is False
    quarantine_alert.assert_awaited_once()
    kwargs = quarantine_alert.await_args.kwargs
    assert kwargs["kb_id"] == kb_id
    assert kwargs["source_type"] == "github"
    assert kwargs["failure_count"] == 3
    assert kwargs["error_message"] == "repo not found"


@pytest.mark.asyncio
async def test_handle_sync_failure_no_alerts_when_persistence_fails(monkeypatch):
    """When the persistence block fails (and rolls back), NO alert is emitted:
    the rollback reverted the failure counter / enabled flag / quarantine
    message, so notifying would describe a state that was never committed.
    The next tick retries and re-emits on a successful pass."""
    # alert=10 so this 10th failure would BOTH escalate and quarantine —
    # proving suppression covers both alert kinds.
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=10)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 10
    )
    source = _make_error_source(sync_failure_count=9)  # would quarantine AND escalate
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    # The activity log raises → the whole try block rolls back.
    monkeypatch.setattr(
        "app.services.kb_helpers.log_kb_activity",
        AsyncMock(side_effect=RuntimeError("db degraded")),
    )
    quarantine_alert = AsyncMock()
    persistent_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", persistent_alert)

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=9,
        prior_perm_fail_count=0,
        error_message="404 Not Found",
        activity_detail="Sync failed: 404 Not Found",
        source_id=str(source.id),
        source_type=source.source_type,
    )

    db.rollback.assert_awaited_once()
    quarantine_alert.assert_not_awaited()
    persistent_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_sync_failure_quarantine_at_above_threshold(monkeypatch):
    """Quarantine fires on the first crossing AND on every subsequent failure
    (the source is disabled, but the alert fires once per call when above
    the threshold, mirroring the stale-reset quarantine pattern)."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0, alert=5)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", 3
    )
    source = _make_error_source(sync_failure_count=5)  # already above threshold
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    quarantine_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_failure_quarantine_alert", quarantine_alert)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=5,
        prior_perm_fail_count=0,
        error_message="still broken",
        activity_detail="Sync failed: still broken",
        source_id=str(source.id),
        source_type=source.source_type,
    )

    assert source.enabled is False
    assert quarantine_alert.await_count == 1


# ── Config-error quarantine ─────────────────────────────────


@pytest.mark.asyncio
async def test_execute_sync_source_disables_on_config_error(monkeypatch):
    """A KBSyncConfigError (e.g. sharing URL as site_id) must disable the
    source immediately, set error status, and NOT re-raise — so the scheduler
    does not increment the retry backoff counter or call _handle_sync_failure."""
    from app.services.kb_sync import KBSyncConfigError

    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="microsoft-drive",
        config={"site_id": "https://tenant.sharepoint.com/:f:/s/MySite/AABB?e=x"},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        # Pre-existing backoff state from earlier transient failures — the
        # config-error disable must clear it so a later re-enable starts
        # from a clean slate.
        sync_failure_count=3,
        sync_stale_count=0,
        sync_perm_fail_count=2,
        sync_next_retry_at=datetime.now(UTC),
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="Old Tender RFP KB")

    async def _cfg_error(**kwargs):
        raise KBSyncConfigError("Sharing link used as site_id — fix the config")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _cfg_error)
    log_activity = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)
    config_error_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_config_error_alert", config_error_alert)

    # Must NOT re-raise — the scheduler must not see an exception.
    result = await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert result == [], "no documents on config error"
    assert source.enabled is False, "source must be disabled on config error"
    assert source.last_sync_status == "error"
    assert source.last_sync_error is not None
    # The force-resync endpoint detects a config-error auto-disable by this
    # prefix (startswith) to re-enable the source after the user fixes the
    # config — pin the contract so the message and detection can't drift.
    assert source.last_sync_error.startswith(kb_sync_scheduler.CONFIG_ERROR_PREFIX)
    # Backoff state must be cleared, not incremented — a disabled source has
    # no scheduled retry, and a later re-enable starts from a clean slate.
    assert source.sync_failure_count == 0
    assert source.sync_next_retry_at is None
    assert source.sync_perm_fail_count == 0


@pytest.mark.asyncio
async def test_execute_sync_source_config_error_emits_alert_exactly_once(monkeypatch):
    """KBSyncConfigError must fire _emit_config_error_alert (which records the
    single sync_config_error activity event) exactly once — the handler must
    NOT log a second copy of the event itself."""
    from app.services.kb_sync import KBSyncConfigError

    kb_id = uuid.uuid4()
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="microsoft-drive",
        config={},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=kb_id, name="KB")

    async def _cfg_error(**kwargs):
        raise KBSyncConfigError("bad config")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _cfg_error)
    log_activity = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)
    config_error_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_emit_config_error_alert", config_error_alert)

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # The handler must NOT log its own sync_config_error activity — the
    # single event comes from _emit_config_error_alert (no duplicates).
    config_error_events = [
        c for c in log_activity.await_args_list if c.args[2] == "sync_config_error"
    ]
    assert config_error_events == [], "handler must not duplicate the activity event"

    # _emit_config_error_alert must be called exactly once with the right context
    config_error_alert.assert_awaited_once()
    call_kwargs = config_error_alert.await_args.kwargs
    assert call_kwargs["kb_id"] == kb_id
    assert call_kwargs["source_type"] == "microsoft-drive"
    assert "bad config" in call_kwargs["error_message"]


@pytest.mark.asyncio
async def test_emit_config_error_alert_records_activity_and_broadcasts(monkeypatch):
    """_emit_config_error_alert must record the durable sync_config_error KB
    activity event and publish the company-wide SSE notification."""
    import app.services.kb_helpers as kb_helpers

    log_activity = AsyncMock()
    monkeypatch.setattr(kb_helpers, "log_kb_activity", log_activity)

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    async def _fake_redis_client():
        return _FakeRedis()

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)

    db = SimpleNamespace(flush=AsyncMock(), begin_nested=_FakeNested)
    kb_id = uuid.uuid4()

    await kb_sync_scheduler._emit_config_error_alert(
        db,
        kb_id=kb_id,
        source_type="microsoft-drive",
        error_message="sharing link used as site_id",
    )

    log_activity.assert_awaited_once()
    call = log_activity.await_args
    assert call.args[1] == kb_id
    assert call.args[2] == "sync_config_error"
    assert "sharing link used as site_id" in call.kwargs["details"]

    assert len(published) == 1
    assert published[0][0] == "activity:company"
    assert "disabled" in published[0][1]


@pytest.mark.asyncio
async def test_execute_sync_source_transient_error_still_retries(monkeypatch):
    """A plain RuntimeError (transient failure) must NOT disable the source —
    only KBSyncConfigError triggers immediate disable. The transient path
    must still increment sync_failure_count and re-raise."""
    _set_retry_settings(monkeypatch, base=300, max_delay=1800, jitter=0)

    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="microsoft-drive",
        config={},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _transient(**kwargs):
        raise RuntimeError("network blip")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _transient)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    monkeypatch.setattr(kb_sync_scheduler, "_emit_config_error_alert", AsyncMock())

    with pytest.raises(RuntimeError, match="network blip"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.enabled is True, "transient error must not disable the source"
    assert source.sync_failure_count == 1, "failure count must be incremented"


# ── Permanent-error (404/410) quarantine ───────────────


def test_is_permanent_sync_error_detects_404():
    """HTTP 404 (the exact error from the OneDrive issue) is detected as
    a permanent, unresolvable configuration error."""
    # Exact httpx error format surfaced in the issue.
    assert kb_sync_scheduler._is_permanent_sync_error(
        "Client error '404 Not Found' for url "
        "'https://graph.microsoft.com/v1.0/me/drive/root:/CloudStorage/:/children'"
    )
    # Bare status-text variants.
    assert kb_sync_scheduler._is_permanent_sync_error("404 Not Found")
    assert kb_sync_scheduler._is_permanent_sync_error("HTTP 404 Not Found")
    # Case-insensitive.
    assert kb_sync_scheduler._is_permanent_sync_error("404 not found")


def test_is_permanent_sync_error_detects_410():
    """HTTP 410 Gone is also detected as permanent (resource permanently removed)."""
    assert kb_sync_scheduler._is_permanent_sync_error(
        "Client error '410 Gone' for url '...'"
    )
    assert kb_sync_scheduler._is_permanent_sync_error("410 Gone")
    assert kb_sync_scheduler._is_permanent_sync_error("410 gone")


def test_is_permanent_sync_error_detects_notion_page_400():
    """Notion stale page references can return 400 and must count as permanent."""
    assert kb_sync_scheduler._is_permanent_sync_error(
        "Client error '400 Bad Request' for url 'https://api.notion.com/v1/pages/abc'",
        source_type="notion",
    )


def test_is_permanent_sync_error_ignores_non_notion_page_400():
    """A generic 400 must not be treated as permanent for non-Notion sources."""
    assert not kb_sync_scheduler._is_permanent_sync_error(
        "Client error '400 Bad Request' for url 'https://api.example.com/v1/items/abc'",
        source_type="microsoft-drive",
    )


def test_is_permanent_sync_error_ignores_transient():
    """Transient errors (5xx, timeouts, rate limits, 403) must NOT be
    classified as permanent so a brief provider outage or a fixable
    permission problem cannot count toward the perm-fail quarantine."""
    assert not kb_sync_scheduler._is_permanent_sync_error(
        "Server error '500 Internal Server Error'"
    )
    assert not kb_sync_scheduler._is_permanent_sync_error(
        "Client error '429 Too Many Requests'"
    )
    assert not kb_sync_scheduler._is_permanent_sync_error("Sync timed out after 1800s")
    assert not kb_sync_scheduler._is_permanent_sync_error("Connection reset by peer")
    assert not kb_sync_scheduler._is_permanent_sync_error(
        "Client error '403 Forbidden' for url '...'"
    )


def test_is_permanent_sync_error_detects_website_all_permanent_marker():
    """The run-level marker sync_website embeds when a zero-document crawl saw
    exclusively permanent failures must count as permanent for website sources —
    and only for website sources."""
    from app.services.kb_helpers import WEBSITE_ALL_PERMANENT_FAILURES_MARKER

    message = (
        "Website source returned no content and "
        f"{WEBSITE_ALL_PERMANENT_FAILURES_MARKER} (3 URL(s) hit 404/403/401, "
        "a malformed URL, or a redirect loop)"
    )
    assert kb_sync_scheduler._is_permanent_sync_error(
        message, source_type="website"
    )
    # The marker is scoped to website sources — it must not leak into the
    # classification of other source types.
    assert not kb_sync_scheduler._is_permanent_sync_error(
        message, source_type="notion"
    )
    assert not kb_sync_scheduler._is_permanent_sync_error(message)


def test_is_permanent_sync_error_ignores_website_transient_tainted_message():
    """The plain failure message sync_website raises for outage-tainted
    zero-document crawls must NOT be classified as permanent, preserving the
    strict 'consecutive' contract of the perm-fail budget."""
    assert not kb_sync_scheduler._is_permanent_sync_error(
        "Website source returned no content: 4 URL fetch(es) failed and "
        "transient errors (timeouts, rate limits) were observed during the "
        "crawl. The site may be temporarily unavailable",
        source_type="website",
    )


def _patch_perm_fail_thresholds(monkeypatch, *, perm=5, general=100):
    """Set the perm-fail threshold and park the general error-count
    quarantine far away so it cannot interfere with the
    perm-fail-specific assertions."""
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_perm_fail_quarantine_threshold", perm
    )
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_failure_quarantine_threshold", general
    )


@pytest.mark.asyncio
async def test_handle_sync_failure_increments_perm_fail_count_on_404(monkeypatch):
    """A 404 error increments ``sync_perm_fail_count`` but does NOT quarantine
    the source while the count is below the threshold."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5)

    source = _make_error_source(sync_failure_count=0, sync_perm_fail_count=2)
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=0,
        prior_perm_fail_count=2,
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    assert source.sync_perm_fail_count == 3  # incremented
    assert source.enabled is True  # not yet quarantined (3 < 5)
    assert (source.last_sync_error or "").startswith(
        "Client error '404 Not Found'"
    )  # still the raw error (not quarantine prefix)


@pytest.mark.asyncio
async def test_handle_sync_failure_quarantines_at_perm_fail_threshold(monkeypatch):
    """When ``sync_perm_fail_count`` reaches the quarantine threshold the source
    is disabled and flagged with the needs-reconfiguration prefix, and the
    root cause survives in the message so the operator can fix the config."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5)

    source = _make_error_source(
        sync_failure_count=4,
        sync_perm_fail_count=4,  # 5th 404 crosses the threshold
    )
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=4,
        prior_perm_fail_count=4,
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    assert source.sync_perm_fail_count == 5
    assert source.enabled is False  # quarantined
    assert source.last_sync_error is not None
    assert source.last_sync_error.startswith(kb_sync_scheduler.PERM_FAIL_ERROR_PREFIX)
    # The quarantine message replaces ``last_sync_error`` (the field the UI
    # surfaces) — the underlying root cause must survive in it.
    assert "404 Not Found" in source.last_sync_error
    assert perm_alert.await_count == 1
    call_kwargs = perm_alert.await_args.kwargs
    assert call_kwargs["perm_fail_count"] == 5
    assert call_kwargs["source_type"] == "microsoft-drive"


@pytest.mark.asyncio
async def test_handle_sync_failure_transient_error_resets_perm_fail_count(monkeypatch):
    """A non-permanent failure (e.g. a timeout) RESETS ``sync_perm_fail_count``
    to 0: the counter tracks *consecutive* 404/410s, so a pattern like
    404, timeout, 404, timeout must never creep toward the threshold."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5)

    source = _make_error_source(
        sync_failure_count=0,
        sync_perm_fail_count=4,  # just below threshold
    )
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=0,
        prior_perm_fail_count=4,
        error_message="Sync timed out after 1800s",  # NOT a permanent error
        activity_detail="sync timed out",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    # The chain of consecutive 404s is broken — counter resets to 0.
    assert source.sync_perm_fail_count == 0
    assert source.enabled is True  # not quarantined
    assert perm_alert.await_count == 0


@pytest.mark.asyncio
async def test_handle_sync_failure_zero_threshold_disables_perm_quarantine(monkeypatch):
    """Setting ``kb_sync_perm_fail_quarantine_threshold = 0`` disables the
    permanent-error quarantine — sources are never auto-disabled by it
    regardless of how many 404s they accumulate."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=0)

    source = _make_error_source(sync_failure_count=0, sync_perm_fail_count=999)
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=0,
        prior_perm_fail_count=999,
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    assert source.sync_perm_fail_count == 1000  # still counted
    assert source.enabled is True  # quarantine disabled
    assert perm_alert.await_count == 0


@pytest.mark.asyncio
async def test_handle_sync_failure_perm_message_wins_when_both_quarantine(monkeypatch):
    """When the SAME failure crosses both the general error-count threshold
    and the perm-fail threshold, the more actionable
    "needs reconfiguration" message wins ``last_sync_error`` and both
    alerts fire."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5, general=10)

    source = _make_error_source(
        sync_failure_count=9,  # 10th failure crosses the general threshold
        sync_perm_fail_count=4,  # 5th 404 crosses the perm threshold
    )
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    general_alert = AsyncMock()
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_failure_quarantine_alert", general_alert
    )
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=9,
        prior_perm_fail_count=4,
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    assert source.enabled is False
    assert (source.last_sync_error or "").startswith(
        kb_sync_scheduler.PERM_FAIL_ERROR_PREFIX
    )
    assert general_alert.await_count == 1
    assert perm_alert.await_count == 1


@pytest.mark.asyncio
async def test_handle_sync_failure_no_perm_alert_when_persistence_fails(monkeypatch):
    """When the persistence block fails (and rolls back), the perm-fail
    quarantine alert is suppressed like the other alerts: the rollback
    reverted the counter / enabled flag / message, so notifying would
    describe a state that was never committed."""
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5)

    source = _make_error_source(
        sync_failure_count=0,
        sync_perm_fail_count=4,  # would quarantine on this 5th 404
    )
    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    # The activity log raises -> the whole try block rolls back.
    monkeypatch.setattr(
        "app.services.kb_helpers.log_kb_activity",
        AsyncMock(side_effect=RuntimeError("db degraded")),
    )
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=0,
        prior_perm_fail_count=4,
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="microsoft-drive",
    )

    db.rollback.assert_awaited_once()
    perm_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_sync_failure_perm_fail_count_survives_post_rollback_expiry(
    monkeypatch,
):
    """Regression test for a failed sync's rollback.

    When a sync fails the caller rolls back the session before invoking
    ``_handle_sync_failure``.  SQLAlchemy's ``session.expire_all()`` (always
    called on rollback, regardless of ``expire_on_commit=False``) removes
    attribute values from the instance's ``__dict__``.  The old code read
    ``source.__dict__.get("sync_perm_fail_count", 0)`` *inside* the helper,
    which silently returned 0 after the rollback and reset the counter on
    every failure — the quarantine never triggered.

    The fix captures ``prior_perm_fail_count`` *before* the early-commit
    rollback cycle and passes it explicitly.  This test verifies that the
    counter correctly accumulates when the source object has NO
    ``sync_perm_fail_count`` key in ``__dict__`` (i.e. as it appears after
    a post-rollback expire_all), but the prior value IS passed via the new
    parameter.  The same contract covers ``source_user_id``:
    a ``__dict__`` read of ``user_id`` inside the helper would also be empty
    post-rollback, so the caller-supplied value must reach the alert.
    """
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)
    _patch_perm_fail_thresholds(monkeypatch, perm=5)

    # Source does NOT have sync_perm_fail_count (or user_id) in __dict__ —
    # simulates the state after SQLAlchemy's session.expire_all() clears
    # attribute values on rollback.
    source = _make_error_source(sync_failure_count=3)
    # Confirm the attributes are truly absent (not just 0 / None).
    assert "sync_perm_fail_count" not in source.__dict__
    assert "user_id" not in source.__dict__

    db = SimpleNamespace(flush=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    # Stub the alert like the sibling perm-quarantine tests: without the stub
    # the quarantine path would run the real notification plumbing (redis,
    # DB lookups) inside a unit test.
    perm_alert = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler, "_emit_perm_fail_quarantine_alert", perm_alert
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_persistent_failure_alert", AsyncMock())

    configurer_id = str(uuid.uuid4())
    # Simulate 4 prior consecutive 404s captured before the rollback.
    await kb_sync_scheduler._handle_sync_failure(
        db,
        source,
        kb_id=source.knowledge_base_id,
        prior_failure_count=3,
        prior_perm_fail_count=4,  # captured BEFORE rollback expired __dict__
        error_message="Client error '404 Not Found' for url '...'",
        activity_detail="sync failed: 404",
        source_id=str(source.id),
        source_type="notion",
        source_user_id=configurer_id,  # likewise captured pre-rollback
    )

    # Counter must use the passed value (4 -> 5), NOT a default 0 (-> 1).
    assert source.sync_perm_fail_count == 5
    assert source.enabled is False, "5th consecutive 404 must quarantine the source"
    assert (source.last_sync_error or "").startswith(
        kb_sync_scheduler.PERM_FAIL_ERROR_PREFIX
    )
    assert perm_alert.await_count == 1
    call_kwargs = perm_alert.await_args.kwargs
    assert call_kwargs["perm_fail_count"] == 5
    # The configuring user survives the rollback via the parameter too.
    assert call_kwargs["source_user_id"] == configurer_id


@pytest.mark.asyncio
async def test_emit_perm_fail_quarantine_alert_is_best_effort(monkeypatch):
    """The perm-fail quarantine alert must never raise, even if both the
    activity log and the SSE broadcast fail (it delegates to the shared
    best-effort ``_emit_sync_notification`` helper)."""
    import app.services.kb_helpers as kb_helpers

    monkeypatch.setattr(
        kb_helpers, "log_kb_activity", AsyncMock(side_effect=RuntimeError("boom"))
    )

    async def _boom_redis():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _boom_redis)

    db = SimpleNamespace(flush=AsyncMock(), begin_nested=_FakeNested)
    result = await kb_sync_scheduler._emit_perm_fail_quarantine_alert(
        db,
        kb_id=uuid.uuid4(),
        source_type="microsoft-drive",
        perm_fail_count=5,
        quarantine_message=(
            "Source needs reconfiguration after 5 consecutive 'not found' errors"
        ),
    )
    assert result is None  # swallowed both failures


@pytest.mark.asyncio
async def test_execute_sync_source_resets_perm_fail_count_on_success(monkeypatch):
    """A successful sync resets ``sync_perm_fail_count`` to 0 so the quarantine
    budget is restored after a previously-failing source recovers."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_stale_source(
        source_type="microsoft-drive",
        config={"folder_path": "/"},
        integration_id=None,
        last_sync_status="error",
        last_synced_at=None,
        last_sync_error="Client error '404 Not Found'",
        sync_failure_count=4,
        sync_perm_fail_count=4,
        sync_next_retry_at=datetime.now(UTC) + timedelta(hours=2),
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="Filigran Presentations")

    async def _fake_heartbeat(source_id):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _fake_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 0)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "success"
    assert source.sync_perm_fail_count == 0, "perm-fail count must reset on success"


def test_perm_fail_error_prefix_contract():
    """Pin the ``PERM_FAIL_ERROR_PREFIX`` string — the force-resync endpoint
    and the re-enable recovery path both match on this prefix to detect
    quarantined sources.  A silent change would break quarantine detection
    without a test failure."""
    assert (
        kb_sync_scheduler.PERM_FAIL_ERROR_PREFIX == "Source needs reconfiguration after"
    )


# ── KB identifiers in log context (actionable sync logs) ──────────────


def test_kb_sync_log_context_binds_and_restores():
    """``kb_sync_log_context`` binds kb/source identifiers as structlog
    contextvars for the duration of the block and restores the previous
    state on exit — so every log record emitted during a sync (including
    the deep provider-level warnings in ``kb_sync.py``) carries the KB id
    and name needed to act on it."""
    import structlog

    from app.services.kb_helpers import kb_sync_log_context

    kb_id = uuid.uuid4()
    src_id = uuid.uuid4()

    assert "kb_id" not in structlog.contextvars.get_contextvars()

    with kb_sync_log_context(
        kb_id=kb_id,
        kb_name="Product Docs",
        sync_source_id=src_id,
        source_type="github",
    ):
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["kb_id"] == str(kb_id)
        assert ctx["kb_name"] == "Product Docs"
        assert ctx["sync_source_id"] == str(src_id)
        assert ctx["source_type"] == "github"

    ctx_after = structlog.contextvars.get_contextvars()
    assert "kb_id" not in ctx_after
    assert "kb_name" not in ctx_after


def test_kb_sync_log_context_skips_none_values():
    """``None`` identifiers are skipped (partial context is fine) instead of
    binding a literal 'None' string."""
    import structlog

    from app.services.kb_helpers import kb_sync_log_context

    with kb_sync_log_context(kb_id=uuid.uuid4(), kb_name=None, source_type=None):
        ctx = structlog.contextvars.get_contextvars()
        assert "kb_id" in ctx
        assert "kb_name" not in ctx
        assert "source_type" not in ctx


@pytest.mark.asyncio
async def test_execute_sync_source_binds_kb_log_context(monkeypatch):
    """The scheduler entry point binds kb_id / kb_name / sync_source_id /
    source_type for the whole sync, so provider-level log records emitted
    inside ``_run_sync_in_isolated_session`` (e.g. the GitHub default-branch
    fallback warning) automatically carry the KB identifiers."""
    import structlog

    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github_public",
        config={"repo": "XTM-One-Platform/xtm-one"},
        integration_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_perm_fail_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="Engineering KB")

    observed_ctx: dict = {}

    async def _fake_sync(**kwargs):
        observed_ctx.update(structlog.contextvars.get_contextvars())
        return ["doc-1"], 0

    async def _fake_heartbeat(source_id):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _fake_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_sync)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert observed_ctx["kb_id"] == str(kb.id)
    assert observed_ctx["kb_name"] == "Engineering KB"
    assert observed_ctx["sync_source_id"] == str(source.id)
    assert observed_ctx["source_type"] == "github_public"
    # Context must not leak past the sync.
    assert "kb_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_reset_stale_syncs_logs_include_kb_id(monkeypatch, caplog):
    """The stale-reset warning must include the knowledge base id inline so
    operators can locate the affected KB without a database lookup."""
    import logging

    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_after_secs", 600)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", 0
    )
    _set_retry_settings(monkeypatch, base=600, max_delay=86_400, jitter=0)

    kb_id = uuid.uuid4()
    source = _make_stale_source(knowledge_base_id=kb_id)

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_SourcesResult([source])),
        flush=AsyncMock(),
    )

    with caplog.at_level(logging.WARNING):
        reset = await kb_sync_scheduler.reset_stale_syncs(db)

    assert reset == 1
    warnings = [
        r.getMessage() for r in caplog.records if "Reset stale" in r.getMessage()
    ]
    assert warnings, "expected a stale-reset warning"
    assert any(str(kb_id) in m for m in warnings), warnings


# ── Personal notification ────────────────────────────────


@pytest.mark.asyncio
async def test_emit_sync_notification_personal_channel_for_user_owned_kb(monkeypatch):
    """When the KB has a private owner, _emit_sync_notification must publish
    to BOTH ``activity:company`` and the owner's personal ``activity:{user_id}``
    channel, AND persist a durable ``Activity`` row for the owner — the
    personal activity feed reads ``Activity`` rows, so the SSE publish alone
    would be invisible."""
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_FakeRedis()))

    owner_id = uuid.uuid4()
    db = SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_FakeNested,
        execute=AsyncMock(return_value=_ScalarResult(owner_id)),
        add=Mock(),
    )
    kb_id = uuid.uuid4()

    await kb_sync_scheduler._emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type="github",
        activity_type="sync_quarantined",
        detail="Source quarantined after 10 consecutive failures",
        summary="A github sync source has been disabled.",
    )

    channels = [ch for ch, _ in published]
    assert "activity:company" in channels, "company-wide broadcast must always fire"
    assert f"activity:{owner_id}" in channels, "personal channel must fire for user-owned KB"
    # Same payload on both channels
    assert len(published) == 2
    payloads = [json.loads(p) for _, p in published]
    assert payloads[0] == payloads[1], "both channels should carry the same payload"

    # Durable personal notification: an Activity row for the owner.
    from app.models.activity import Activity

    added = [a for (a,), _kw in db.add.call_args_list if isinstance(a, Activity)]
    assert len(added) == 1, "a durable Activity row must be persisted for the owner"
    assert added[0].user_id == owner_id
    assert added[0].activity_type == "notification"
    assert added[0].summary == "A github sync source has been disabled."
    assert added[0].extra_data["kb_id"] == str(kb_id)


@pytest.mark.asyncio
async def test_emit_sync_notification_company_only_when_no_owner(monkeypatch):
    """When the KB has no owner (company-managed, ``user_id`` is NULL) and no
    source_user_id fallback is supplied, ``_emit_sync_notification`` must
    publish to ``activity:company`` only — no personal channel and no
    durable Activity row."""
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_FakeRedis()))

    db = SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_FakeNested,
        # Simulate a company-managed KB (user_id is NULL → scalar_one_or_none returns None)
        execute=AsyncMock(return_value=_ScalarResult(None)),
        add=Mock(),
    )

    await kb_sync_scheduler._emit_sync_notification(
        db,
        kb_id=uuid.uuid4(),
        source_type="github",
        activity_type="sync_quarantined",
        detail="Source quarantined after 10 consecutive failures",
        summary="A github sync source has been disabled.",
    )

    assert len(published) == 1
    assert published[0][0] == "activity:company"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_emit_sync_notification_falls_back_to_source_user_for_company_kb(
    monkeypatch,
):
    """A company-managed KB (``user_id`` NULL) with a ``source_user_id``
    fallback must notify the sync source's configuring user personally —
    both the durable Activity row and the personal SSE channel."""
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_FakeRedis()))

    configurer_id = uuid.uuid4()
    db = SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_FakeNested,
        # Company-managed KB: owner lookup returns NULL.
        execute=AsyncMock(return_value=_ScalarResult(None)),
        add=Mock(),
    )

    await kb_sync_scheduler._emit_sync_notification(
        db,
        kb_id=uuid.uuid4(),
        source_type="github",
        activity_type="sync_quarantined",
        detail="Source quarantined after 10 consecutive failures",
        summary="A github sync source has been disabled.",
        source_user_id=str(configurer_id),
    )

    channels = [ch for ch, _ in published]
    assert "activity:company" in channels
    assert f"activity:{configurer_id}" in channels, (
        "the source's configuring user must be notified for company-managed KBs"
    )

    from app.models.activity import Activity

    added = [a for (a,), _kw in db.add.call_args_list if isinstance(a, Activity)]
    assert len(added) == 1
    assert added[0].user_id == configurer_id


@pytest.mark.asyncio
async def test_emit_sync_notification_owner_lookup_failure_falls_back_to_company(
    monkeypatch,
):
    """If the DB owner lookup raises an exception, ``_emit_sync_notification``
    must still publish to ``activity:company`` — the personal channel is
    silently skipped rather than propagating the error."""
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_FakeRedis()))

    db = SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_FakeNested,
        execute=AsyncMock(side_effect=RuntimeError("db exploded")),
    )

    result = await kb_sync_scheduler._emit_sync_notification(
        db,
        kb_id=uuid.uuid4(),
        source_type="github",
        activity_type="sync_quarantined",
        detail="Source quarantined after 10 consecutive failures",
        summary="A github sync source has been disabled.",
    )

    assert result is None  # best-effort: no exception propagated
    assert len(published) == 1
    assert published[0][0] == "activity:company"
