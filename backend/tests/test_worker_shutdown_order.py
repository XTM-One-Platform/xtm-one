"""Regression tests for worker shutdown ordering.

Root cause being pinned: ``shutdown_default_executor()`` was called BEFORE
``close_queue()`` in the worker's SAQ shutdown hook.  redis-py's async cleanup
paths (pubsub close, connection-pool disconnect, SSL teardown) can internally
call ``loop.run_in_executor(None, ...)``; when the default ``ThreadPoolExecutor``
is already shut down at that point, redis-py raises
``RuntimeError: cannot schedule new futures after shutdown``, which it wraps as
``redis.exceptions.ConnectionError: cannot schedule new futures after shutdown``.

This appeared on *every* pod rolling-restart in production (4/4 terminations in
a 25-minute window) and was caused by two issues:
1. Executor shut down before queue closed — direct source of the exception.
2. No ``shutdown_grace_period_s`` configured for the SAQ worker (effectively 0),
   so *any* in-flight job was immediately force-cancelled on SIGTERM.

These tests ensure neither regression re-enters the codebase.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, settings
from tests.ast_helpers import call_lines, function_node

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# Kubernetes' default ``terminationGracePeriodSeconds``.  SAQ only runs the
# worker's shutdown hook (Langfuse flush, Redis queue close) AFTER the grace
# period elapses, so the shipped default must leave teardown headroom below
# this deadline — otherwise a busy pod is SIGKILLed mid-shutdown and the
# graceful teardown (including the close_queue ordering fix) never runs.
_K8S_DEFAULT_TERMINATION_GRACE_S = 30


# ---------------------------------------------------------------------------
# Shutdown call-order pin (AST inspection)
# ---------------------------------------------------------------------------


def test_close_queue_called_before_shutdown_executor_in_worker_shutdown():
    """``close_queue()`` must appear BEFORE ``shutdown_default_executor()`` in
    the worker's SAQ shutdown hook.

    redis-py's async cleanup (pubsub close, connection-pool disconnect) can use
    ``loop.run_in_executor()`` internally.  If the default ThreadPoolExecutor
    is already shut down at that moment, redis-py raises
    ``RuntimeError: cannot schedule new futures after shutdown``, which it
    wraps as ``redis.exceptions.ConnectionError`` — the traceback seen on
    every rolling-restart pod termination.

    Closing the queue first guarantees all Redis I/O completes while the
    executor is still alive.
    """
    shutdown = function_node(APP_DIR / "worker.py", "shutdown")

    close_lines = call_lines(shutdown, "close_queue")
    executor_lines = call_lines(shutdown, "shutdown_default_executor")

    assert close_lines, "worker shutdown hook must call close_queue()"
    assert executor_lines, "worker shutdown hook must call shutdown_default_executor()"

    first_close = min(close_lines)
    first_executor = min(executor_lines)

    assert first_close < first_executor, (
        f"close_queue() (line {first_close}) must be called BEFORE "
        f"shutdown_default_executor() (line {first_executor}) in the worker "
        "shutdown hook — for why the reversed order causes "
        "redis.exceptions.ConnectionError on every rolling restart"
    )


# ---------------------------------------------------------------------------
# Grace-period setting
# ---------------------------------------------------------------------------


def test_worker_settings_has_shutdown_grace_period():
    """The SAQ ``settings`` dict must include a ``shutdown_grace_period_s``
    wired from config so in-flight jobs have a window to complete cleanly
    before SAQ sends cancellation signals on SIGTERM.

    Without the key, SAQ's default grace period is None / 0 — any job still
    running when the pod receives SIGTERM is *immediately* cancelled.

    The value is asserted non-negative (not strictly positive) because
    ``WORKER_SHUTDOWN_GRACE_PERIOD=0`` is the documented explicit opt-out —
    the suite must pass under any *valid* configuration.  The shipped
    default's positivity is pinned separately by
    ``test_default_grace_period_leaves_teardown_headroom`` on the field
    default, which env overrides cannot mask.
    """
    from app import worker

    grace = worker.settings.get("shutdown_grace_period_s")
    assert grace is not None, (
        "SAQ settings must include 'shutdown_grace_period_s' — "
        "without it SAQ uses a 0-second grace period and every in-flight "
        "job is immediately cancelled on SIGTERM"
    )
    assert isinstance(grace, int) and grace >= 0, (
        f"shutdown_grace_period_s must be a non-negative integer "
        f"(0 = documented explicit opt-out), got {grace!r}"
    )
    # Pin the WIRING, not just the presence: the SAQ value must come from the
    # config setting so operators tuning WORKER_SHUTDOWN_GRACE_PERIOD actually
    # affect the worker (a future hardcode would pass the positivity check
    # above while silently orphaning the setting).
    assert grace == settings.worker_shutdown_grace_period, (
        "SAQ 'shutdown_grace_period_s' must be wired from "
        f"settings.worker_shutdown_grace_period ({settings.worker_shutdown_grace_period!r}), "
        f"got {grace!r}"
    )


def test_config_exposes_worker_shutdown_grace_period():
    """The ``worker_shutdown_grace_period`` config setting must be a
    non-negative int so operators can tune the grace window via environment
    variable (``WORKER_SHUTDOWN_GRACE_PERIOD``).

    ``0`` is the documented explicit opt-out (immediate cancellation), so the
    env-resolved value is only asserted non-negative here — negatives are
    already rejected at startup by the field validator (covered by
    ``test_negative_grace_period_fails_at_startup``), and the shipped
    default's positivity is pinned on the field default by
    ``test_default_grace_period_leaves_teardown_headroom``."""
    grace = settings.worker_shutdown_grace_period
    assert isinstance(grace, int) and grace >= 0, (
        f"settings.worker_shutdown_grace_period must be a non-negative int "
        f"(0 = documented explicit opt-out), got {grace!r}"
    )


def test_negative_grace_period_fails_at_startup():
    """A negative ``WORKER_SHUTDOWN_GRACE_PERIOD`` must fail Settings
    validation at startup instead of silently reintroducing the
    immediate-cancellation behaviour this feature fixes (SAQ treats any
    falsy grace as "no grace period").  ``0`` remains the documented
    explicit opt-out."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_shutdown_grace_period=-1)

    cfg = Settings(_env_file=None, worker_shutdown_grace_period=0)
    assert cfg.worker_shutdown_grace_period == 0


def test_default_grace_period_leaves_teardown_headroom():
    """The SHIPPED default grace period must sit strictly below Kubernetes'
    default ``terminationGracePeriodSeconds`` (30 s) with room for teardown.

    SAQ's ``Worker.stop()`` waits the full grace period for in-flight jobs,
    THEN cancels them (1 s hard deadline), THEN runs the shutdown hook that
    closes the Redis queue and flushes traces.  If the default grace equals or
    exceeds the pod's kill deadline, a busy worker is SIGKILLed before the
    hook runs — silently losing the graceful teardown this feature exists to
    provide.  Asserts the *field default* (not the env-resolved value) so a
    CI environment override cannot mask a bad shipped default.
    """
    default_grace = Settings.model_fields["worker_shutdown_grace_period"].default
    assert 0 < default_grace <= _K8S_DEFAULT_TERMINATION_GRACE_S - 5, (
        f"worker_shutdown_grace_period default ({default_grace}s) must leave "
        f"at least ~5s of teardown headroom below Kubernetes' default "
        f"terminationGracePeriodSeconds ({_K8S_DEFAULT_TERMINATION_GRACE_S}s); "
        "SAQ runs the shutdown hook (Redis queue close, trace flush) only "
        "after the grace elapses"
    )
