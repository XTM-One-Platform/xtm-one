"""Contract tests for the SAQ queue Redis hardening.

Root cause being pinned: the SAQ queue client used to be built with
``Queue.from_url(REDIS_URL)`` — plain redis-py defaults, i.e. no TCP
keepalive, no health checks — and the worker ran with SAQ's default
``dequeue_timeout=0`` (a ``BLMOVE`` that blocks FOREVER on a single
read).  A Redis connection dying silently (network blip, NAT/idle
reset, container restart, host sleep) therefore hung every worker
slot indefinitely with zero errors while enqueued document-embedding
jobs piled up — the platform-wide "all re-embedding stuck for >24h,
worker shows no errors" incident.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core import queue as queue_module
from app.core.config import settings


def test_tcp_keepalive_options_present_and_sane():
    options = queue_module._tcp_keepalive_options()
    # At least one keepalive knob must exist on every supported platform
    # (TCP_KEEPIDLE on Linux/Windows, TCP_KEEPALIVE on macOS).
    assert options, "no TCP keepalive constants resolved on this platform"
    assert all(isinstance(v, int) and v > 0 for v in options.values())


def test_saq_connection_kwargs_never_set_socket_timeout_or_decoding():
    """``socket_timeout`` would fire on every healthy blocking BLMOVE /
    pub-sub read so it must stay out; ``decode_responses`` must be an
    EXPLICIT ``False`` (not merely absent) because SAQ's ``iter_jobs``
    calls ``job_id.decode("utf-8")`` on SCAN results — a string-mode
    client breaks job iteration/cancellation with ``AttributeError:
    'str' object has no attribute 'decode'``, and redis-py's
    ``from_url`` would otherwise honour a ``?decode_responses=True``
    URL query param.
    """
    kwargs = queue_module._saq_connection_kwargs()
    assert "socket_timeout" not in kwargs
    assert kwargs["decode_responses"] is False
    assert kwargs["socket_keepalive"] is True
    assert kwargs["socket_keepalive_options"] == queue_module._tcp_keepalive_options()
    assert kwargs["health_check_interval"] == settings.redis_health_check_interval
    assert kwargs["socket_connect_timeout"] == settings.redis_socket_connect_timeout


def test_get_queue_standalone_builds_hardened_client(monkeypatch):
    """``get_queue()`` must construct the redis client itself with the
    hardened kwargs — ``Queue.from_url`` forwards kwargs to ``RedisQueue``,
    NOT to the redis client, so the old code silently dropped any attempt
    to harden through it.
    """
    import redis.asyncio as redis_asyncio

    captured: dict = {}

    def _fake_from_url(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(queue_module, "_queue", None)
    monkeypatch.setattr(redis_asyncio, "from_url", _fake_from_url)
    monkeypatch.setattr(queue_module.settings, "redis_mode", "standalone")

    q = queue_module.get_queue()

    assert q is not None
    assert captured["url"] == queue_module.settings.redis_url
    assert captured["socket_keepalive"] is True
    assert captured["socket_keepalive_options"]
    assert "socket_timeout" not in captured
    # Explicit False (kwargs beat URL query options in redis-py) so a
    # REDIS_URL carrying ``?decode_responses=True`` cannot break SAQ's
    # bytes contract.
    assert captured["decode_responses"] is False


def test_worker_settings_bound_dequeue_and_no_bogus_timers():
    """The worker must run with a bounded dequeue, and must NOT carry the
    old ``timers: {"job_timeout": ...}`` entry — ``job_timeout`` is not a
    SAQ timer key (valid: schedule / worker_info / sweep / abort), so it
    was silently ignored while implying a protection that never existed.
    Per-job timeouts are set at enqueue time via ``Job(timeout=...)``.
    """
    from app import worker

    dequeue_timeout = worker.settings.get("dequeue_timeout")
    assert dequeue_timeout is not None and dequeue_timeout > 0, (
        "dequeue_timeout must be a positive bound — SAQ's default of 0 "
        "blocks forever on a single BLMOVE read, which is how a silently "
        "dead Redis connection froze the whole worker with no errors"
    )

    timers = worker.settings.get("timers") or {}
    assert "job_timeout" not in timers, (
        "'job_timeout' is not a valid SAQ timer key and is silently "
        "ignored — configure per-job timeouts on Job(timeout=...) instead"
    )
    valid_timer_keys = {"schedule", "worker_info", "sweep", "abort"}
    assert set(timers) <= valid_timer_keys


# ---------------------------------------------------------------------------
# Teardown-safe disconnect
# ---------------------------------------------------------------------------
#
# SAQ's CLI runner (``saq.worker.start`` -> ``worker_start``) wraps
# ``worker.start()`` in ``try/finally: await worker.queue.disconnect()``.
# That library-level ``finally`` runs AFTER the app shutdown hook — i.e.
# after ``close_queue()`` already disconnected the queue AND after
# ``shutdown_default_executor()`` killed the loop's default executor.  On the
# second ``disconnect()``, ``pubsub.punsubscribe()`` makes redis-py RECONNECT
# (``PubSub.execute_command`` connects unconditionally), and the reconnect's
# DNS lookup (``loop.run_in_executor``) hits the dead executor:
# ``RuntimeError: cannot schedule new futures after shutdown``, wrapped by
# redis-py as ``redis.exceptions.ConnectionError`` — an uncaught stderr
# traceback on every rolling restart.  Ordering inside the hook cannot reach
# SAQ's own second disconnect; the app-level fix
# is an idempotent-per-connect-cycle disconnect on the queue subclass.


def _make_safe_queue():
    cls = queue_module._teardown_safe_queue_cls()
    return cls(MagicMock())


@pytest.mark.asyncio
async def test_queue_disconnect_is_idempotent_per_connect_cycle(monkeypatch):
    """The second ``disconnect()`` in the same connect cycle must be a no-op
    — that second call is SAQ's own CLI ``finally`` block, which runs after
    the default executor is already shut down."""
    from unittest.mock import AsyncMock

    base_disconnect = AsyncMock()
    base_connect = AsyncMock()
    monkeypatch.setattr("saq.queue.redis.RedisQueue.disconnect", base_disconnect)
    monkeypatch.setattr("saq.queue.base.Queue.connect", base_connect)

    q = _make_safe_queue()

    await q.disconnect()
    await q.disconnect()  # SAQ's CLI finally block — must be a no-op
    assert base_disconnect.await_count == 1

    # A legitimate reconnect re-arms the disconnect (SAQ's health-check CLI
    # does connect -> info -> disconnect on the same queue object).
    await q.connect()
    await q.disconnect()
    assert base_disconnect.await_count == 2


@pytest.mark.asyncio
async def test_queue_disconnect_swallows_teardown_race_errors(monkeypatch, caplog):
    """A teardown race (executor/loop already shutting down) must never
    surface as an unhandled traceback: ``RuntimeError`` and
    ``redis.exceptions.ConnectionError`` carrying a known teardown-race
    signature are suppressed with a DEBUG log (not warning — this fires on
    every rolling restart and must not become recurring log noise)."""
    import logging
    from unittest.mock import AsyncMock

    from redis.exceptions import ConnectionError as RedisConnectionError

    for exc in (
        RuntimeError("cannot schedule new futures after shutdown"),
        RedisConnectionError("cannot schedule new futures after shutdown"),
        RuntimeError("Event loop is closed"),
    ):
        monkeypatch.setattr(
            "saq.queue.redis.RedisQueue.disconnect", AsyncMock(side_effect=exc)
        )
        q = _make_safe_queue()
        with caplog.at_level(logging.DEBUG, logger="app.core.queue"):
            caplog.clear()
            await q.disconnect()  # must not raise
        assert not [r for r in caplog.records if r.levelno > logging.DEBUG]
        # A suppressed teardown error still counts as a completed disconnect
        # attempt — the connection is gone either way, so the guard must be
        # armed and the follow-up call (SAQ's CLI finally) must be a no-op.
        assert q._app_disconnected is True


@pytest.mark.asyncio
async def test_queue_disconnect_logs_unknown_connection_errors_at_warning(
    monkeypatch, caplog
):
    """A ``ConnectionError`` WITHOUT a teardown-race signature (e.g. a
    genuine network failure surfacing during disconnect) is still swallowed —
    raising during a disconnect would only recreate the unhandled traceback —
    but it must be logged at WARNING so real connection problems stay visible
    in default log levels."""
    import logging
    from unittest.mock import AsyncMock

    from redis.exceptions import ConnectionError as RedisConnectionError

    monkeypatch.setattr(
        "saq.queue.redis.RedisQueue.disconnect",
        AsyncMock(side_effect=RedisConnectionError("Connection refused")),
    )
    q = _make_safe_queue()
    with caplog.at_level(logging.DEBUG, logger="app.core.queue"):
        await q.disconnect()  # still must not raise
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_queue_disconnect_propagates_unexpected_errors(monkeypatch):
    """The suppression belt is scoped to the known teardown race — anything
    else must keep propagating so real failures stay visible.  A propagating
    failure must NOT arm the idempotency guard: the underlying disconnect
    never completed, so a retry on the same instance must be allowed to
    attempt the real cleanup again."""
    from unittest.mock import AsyncMock

    base_disconnect = AsyncMock(side_effect=ValueError("boom"))
    monkeypatch.setattr("saq.queue.redis.RedisQueue.disconnect", base_disconnect)
    q = _make_safe_queue()
    with pytest.raises(ValueError):
        await q.disconnect()
    assert q._app_disconnected is False  # guard not armed — retry allowed

    # A retry after the transient failure performs the REAL disconnect and
    # only then arms the guard.
    base_disconnect.side_effect = None
    await q.disconnect()
    assert base_disconnect.await_count == 2
    assert q._app_disconnected is True
    await q.disconnect()  # now idempotent again
    assert base_disconnect.await_count == 2


def test_get_queue_builds_teardown_safe_queue(monkeypatch):
    """``get_queue()`` must construct the teardown-safe subclass so BOTH the
    worker and the platform get the idempotent disconnect."""
    import redis.asyncio as redis_asyncio

    monkeypatch.setattr(queue_module, "_queue", None)
    monkeypatch.setattr(redis_asyncio, "from_url", lambda url, **kw: MagicMock())
    monkeypatch.setattr(queue_module.settings, "redis_mode", "standalone")

    q = queue_module.get_queue()

    assert isinstance(q, queue_module._teardown_safe_queue_cls())


def test_get_queue_sentinel_builds_teardown_safe_queue(monkeypatch):
    """The sentinel branch shares the same subclass — pin it independently so
    a future refactor cannot silently revert one branch to plain
    ``RedisQueue`` while the standalone test stays green."""
    import app.core.redis as redis_core

    sentinel = MagicMock()
    sentinel.master_for.return_value = MagicMock()

    monkeypatch.setattr(queue_module, "_queue", None)
    monkeypatch.setattr(redis_core, "sentinel_client", lambda: sentinel)
    monkeypatch.setattr(queue_module.settings, "redis_mode", "sentinel")

    q = queue_module.get_queue()

    assert isinstance(q, queue_module._teardown_safe_queue_cls())


@pytest.mark.asyncio
async def test_full_teardown_ordering_hook_then_saq_finally(monkeypatch):
    """End-to-end simulation of the production teardown sequence at the queue
    seam: the app shutdown hook (``close_queue``) disconnects first, then
    SAQ's CLI ``finally`` calls ``disconnect()`` on the SAME instance it still
    references (``worker.queue`` is the ``get_queue()`` singleton).  The
    second call must be a no-op — no second real disconnect, hence no pubsub
    reconnect after the executor is gone."""
    from unittest.mock import AsyncMock

    import redis.asyncio as redis_asyncio

    base_disconnect = AsyncMock()
    monkeypatch.setattr("saq.queue.redis.RedisQueue.disconnect", base_disconnect)
    monkeypatch.setattr(queue_module, "_queue", None)
    monkeypatch.setattr(redis_asyncio, "from_url", lambda url, **kw: MagicMock())
    monkeypatch.setattr(queue_module.settings, "redis_mode", "standalone")

    q = queue_module.get_queue()  # what SAQ's worker.queue references

    await queue_module.close_queue()  # app shutdown hook — first disconnect
    assert base_disconnect.await_count == 1
    assert queue_module._queue is None

    await q.disconnect()  # SAQ's CLI finally on the retained reference
    assert base_disconnect.await_count == 1  # no-op: no reconnect possible


@pytest.mark.asyncio
async def test_close_queue_is_idempotent(monkeypatch):
    closed = {"count": 0}

    class _FakeQueue:
        async def disconnect(self):
            closed["count"] += 1

    monkeypatch.setattr(queue_module, "_queue", _FakeQueue())
    await queue_module.close_queue()
    await queue_module.close_queue()

    assert closed["count"] == 1
    assert queue_module._queue is None
