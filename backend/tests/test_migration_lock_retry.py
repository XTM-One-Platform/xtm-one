"""Startup migration runner: retry on transient lock contention.

Regression tests for the 2026-07-05 production outage: a migration's
``SET LOCAL lock_timeout`` safety belt fired (SQLSTATE 55P03,
"canceling statement due to lock timeout") while the ALTER queued behind
a long-lived transaction's ``ACCESS SHARE`` lock — and the startup
runner treated that transient failure as terminal, leaving every pod
permanently unready.

``_apply_pending_migrations`` must now:
- retry 55P03 / 57014 (lock/statement timeout) failures with backoff,
- fail immediately on any non-lock error (genuine schema bug),
- hard-fail only after the retry budget is exhausted.

No database required — ``command.upgrade`` is monkeypatched.
"""

from unittest.mock import patch

import pytest

from app.core import migrations as mig


class _FakeLockTimeoutError(Exception):
    """Mimics the asyncpg lock-timeout error shape (sqlstate attribute)."""

    sqlstate = "55P03"


def _make_upgrade_stub(fail_times: int, exc_factory):
    """Return a ``command.upgrade``-shaped stub failing *fail_times* times."""
    calls = {"count": 0}

    def _upgrade(config, revision):
        calls["count"] += 1
        if calls["count"] <= fail_times:
            raise exc_factory()

    return _upgrade, calls


@pytest.mark.asyncio
async def test_retries_transient_lock_timeout_then_succeeds():
    upgrade, calls = _make_upgrade_stub(2, _FakeLockTimeoutError)
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        patch.object(mig, "_MIGRATION_RETRY_BASE_DELAY", 0.0),
        patch.object(mig, "_MIGRATION_RETRY_MAX_DELAY", 0.0),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_retries_lock_timeout_by_message_probe():
    """Detection must also work when only the error text is available."""

    def _exc():
        return RuntimeError("canceling statement due to lock timeout")

    upgrade, calls = _make_upgrade_stub(1, _exc)
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        patch.object(mig, "_MIGRATION_RETRY_BASE_DELAY", 0.0),
        patch.object(mig, "_MIGRATION_RETRY_MAX_DELAY", 0.0),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_non_lock_error_fails_immediately():
    upgrade, calls = _make_upgrade_stub(
        99, lambda: ValueError("relation does not exist")
    )
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        pytest.raises(RuntimeError, match="refusing to start"),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_sustained_contention_exhausts_attempts_and_raises():
    upgrade, calls = _make_upgrade_stub(999, _FakeLockTimeoutError)
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        patch.object(mig, "_MIGRATION_RETRY_BASE_DELAY", 0.0),
        patch.object(mig, "_MIGRATION_RETRY_MAX_DELAY", 0.0),
        pytest.raises(RuntimeError, match="sustained lock contention"),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == mig._MIGRATION_MAX_ATTEMPTS


class _FakeStatementTimeoutError(Exception):
    sqlstate = "57014"


@pytest.mark.asyncio
async def test_a_statement_timeout_is_retried_once_then_succeeds():
    upgrade, calls = _make_upgrade_stub(1, _FakeStatementTimeoutError)
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        patch.object(mig, "_MIGRATION_RETRY_BASE_DELAY", 0.0),
        patch.object(mig, "_MIGRATION_RETRY_MAX_DELAY", 0.0),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_a_statement_too_long_for_its_bound_is_not_re_run_until_the_attempts_run_out():
    """Each attempt waits out the whole bound; the error names the setting."""
    upgrade, calls = _make_upgrade_stub(999, _FakeStatementTimeoutError)
    with (
        patch.object(mig.command, "upgrade", side_effect=upgrade),
        patch.object(mig, "_MIGRATION_RETRY_BASE_DELAY", 0.0),
        patch.object(mig, "_MIGRATION_RETRY_MAX_DELAY", 0.0),
        pytest.raises(RuntimeError, match="MIGRATION_STATEMENT_TIMEOUT_SECONDS"),
    ):
        await mig._apply_pending_migrations()
    assert calls["count"] == mig._MIGRATION_STATEMENT_TIMEOUT_RETRIES + 1
