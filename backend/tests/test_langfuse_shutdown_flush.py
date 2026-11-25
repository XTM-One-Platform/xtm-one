"""Tests for Langfuse shutdown flushing (assessment finding OBS-1).

The Langfuse v4 SDK batches spans through an OpenTelemetry batch span
processor; spans still queued in the background exporter are silently lost
if the process exits without a flush.  Historically the only flush lived
inside ``reset_langfuse_client()`` (config changes only) — neither the app
lifespan shutdown nor the SAQ worker shutdown flushed Langfuse, so
OTEL-batched spans were dropped on SIGTERM / deploys.

Covers:
- ``flush_langfuse()`` semantics: flush without reset, no-op when disabled,
  never raises.
- ``reset_langfuse_client()`` / ``areset_langfuse_client()`` flush the old
  client and never shut it down.
- Source-inspection pins: the app lifespan shutdown (``app/main.py``) and the
  worker shutdown hook (``app/worker.py``) both call ``flush_langfuse``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from app.services import langfuse_tracing as lft
from tests.ast_helpers import call_lines as _call_lines
from tests.ast_helpers import function_node as _function_node

APP_DIR = Path(lft.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# flush_langfuse semantics
# ---------------------------------------------------------------------------


def test_flush_langfuse_is_noop_when_client_not_initialized(monkeypatch):
    """No cached client (Langfuse disabled / never configured) → silent no-op."""
    monkeypatch.setattr(lft, "_langfuse_client", None)
    # Must not raise.
    lft.flush_langfuse()


def test_flush_langfuse_flushes_without_resetting_client(monkeypatch):
    """``flush_langfuse`` must flush the buffered events but keep the cached
    client + enabled state intact — unlike ``reset_langfuse_client`` it runs
    on every shutdown and must not force a re-initialization round-trip."""
    fake_client = MagicMock()
    monkeypatch.setattr(lft, "_langfuse_client", fake_client)
    monkeypatch.setattr(lft, "_langfuse_enabled", True)

    lft.flush_langfuse()

    fake_client.flush.assert_called_once()
    fake_client.shutdown.assert_not_called()
    # Cache state untouched.
    assert lft._langfuse_client is fake_client
    assert lft._langfuse_enabled is True


def test_flush_langfuse_never_raises_on_flush_failure(monkeypatch):
    """Shutdown paths must not fail on observability cleanup — a flush error
    (network down, exporter already closed) is logged and swallowed."""
    fake_client = MagicMock()
    fake_client.flush.side_effect = RuntimeError("exporter unreachable")
    monkeypatch.setattr(lft, "_langfuse_client", fake_client)

    # Must not raise.
    lft.flush_langfuse()
    fake_client.flush.assert_called_once()


def test_reset_langfuse_client_flushes_without_shutting_down(monkeypatch):
    """``reset_langfuse_client`` drains buffered events through
    ``flush_langfuse`` and clears the cache, but never shuts the client down:
    its resource manager is the SDK's process-wide one for the key, which the
    next ``Langfuse(...)`` with that key gets back (a shut-down one would have
    no consumers, and its later flushes wait forever)."""
    fake_client = MagicMock()
    monkeypatch.setattr(lft, "_langfuse_client", fake_client)
    monkeypatch.setattr(lft, "_langfuse_enabled", True)

    lft.reset_langfuse_client()

    fake_client.flush.assert_called_once()
    fake_client.shutdown.assert_not_called()
    assert lft._langfuse_client is None
    assert lft._langfuse_enabled is None


async def test_areset_langfuse_client_flushes_on_the_langfuse_pool(monkeypatch):
    """The async reset flushes the detached client on the Langfuse pool, never
    the default executor, and never shuts it down."""
    import threading

    flushed_on: list[str] = []
    fake_client = MagicMock()
    fake_client.flush.side_effect = lambda: flushed_on.append(threading.current_thread().name)
    monkeypatch.setattr(lft, "_langfuse_client", fake_client)
    monkeypatch.setattr(lft, "_langfuse_enabled", True)
    monkeypatch.setattr(lft, "_langfuse_init_lock", None)

    await lft.areset_langfuse_client()

    assert len(flushed_on) == 1 and flushed_on[0].startswith("xtmone-langfuse")
    fake_client.shutdown.assert_not_called()
    assert lft._langfuse_client is None


# ---------------------------------------------------------------------------
# Shutdown call sites (source inspection — importing app.main builds the
# whole FastAPI app, so we pin the call sites via AST; helpers shared with
# test_worker_shutdown_order via tests.ast_helpers)
# ---------------------------------------------------------------------------


def test_app_lifespan_shutdown_flushes_langfuse():
    """The FastAPI lifespan must flush Langfuse on shutdown so OTEL-batched
    spans survive SIGTERM / deploys (OBS-1).  A refactor that drops the
    ``flush_langfuse`` call from the lifespan trips here."""
    lifespan = _function_node(APP_DIR / "main.py", "lifespan")
    flush_lines = _call_lines(lifespan, "flush_langfuse")
    assert flush_lines, (
        "app lifespan shutdown must call flush_langfuse() so buffered "
        "Langfuse spans are exported before the process exits"
    )
    # The flush belongs to the shutdown phase — after the lifespan yield.
    # Resolved via AST (not string search) so the word "yield" in a comment
    # or string cannot skew the position check.  Asserts that *some* flush
    # call lives after the yield (not that the *first* one does) so a future
    # startup-phase flush before the yield cannot mask the shutdown flush
    # this test pins.
    yield_lines = [
        sub.lineno for sub in ast.walk(lifespan) if isinstance(sub, ast.Yield)
    ]
    assert yield_lines, "lifespan must contain a yield statement"
    yield_line = min(yield_lines)
    assert any(line > yield_line for line in flush_lines), (
        "flush_langfuse must run in the shutdown phase (after the yield)"
    )


def test_worker_shutdown_flushes_langfuse():
    """The SAQ worker shutdown hook must flush Langfuse so spans emitted by
    assignment runs / sub-agent tasks are not lost on worker restarts."""
    shutdown = _function_node(APP_DIR / "worker.py", "shutdown")
    assert _call_lines(shutdown, "flush_langfuse"), (
        "worker shutdown hook must call flush_langfuse() so buffered "
        "Langfuse spans are exported before the worker exits"
    )


def test_worker_shutdown_drains_tracing_dispatcher():
    """The SAQ worker shutdown hook must call ``shutdown_tracing_dispatcher``
    so the ``_worker_loop`` asyncio background task is stopped cleanly and
    all buffered local traces are flushed before the executor and Redis
    connections are torn down ('Task was destroyed but it
    is pending!' and 'redis.exceptions.ConnectionError: cannot schedule new
    futures after shutdown' on worker exit)."""
    shutdown = _function_node(APP_DIR / "worker.py", "shutdown")
    dispatcher_lines = _call_lines(shutdown, "shutdown_tracing_dispatcher")
    assert dispatcher_lines, (
        "worker shutdown hook must call shutdown_tracing_dispatcher() so the "
        "_worker_loop task is stopped cleanly and buffered traces are flushed "
        "before the executor and Redis connections are torn down"
    )
    # The ORDER is the actual bug: the dispatcher drain performs Redis I/O
    # that schedules work on the default executor, so it must complete before
    # shutdown_default_executor() runs.  Pin the relative order so a refactor
    # cannot keep the call but move it after the executor teardown.
    executor_lines = _call_lines(shutdown, "shutdown_default_executor")
    assert executor_lines, (
        "worker shutdown hook must call shutdown_default_executor() "
        "(expected by the ordering assertion below)"
    )
    assert max(dispatcher_lines) < min(executor_lines), (
        "shutdown_tracing_dispatcher() must run BEFORE "
        "shutdown_default_executor() — draining after the executor is shut "
        "down reintroduces 'cannot schedule new futures after shutdown'"
    )


def test_worker_startup_seeds_otel_exporter_defaults():
    """The SAQ worker startup hook must seed OTEL exporter defaults before
    any Langfuse/OTEL machinery can be initialized — the worker-process
    equivalent of the ``_seed_langfuse_otel_exporter_defaults()`` call in the
    FastAPI lifespan.  ``get_langfuse_client`` also seeds lazily, but seeding
    eagerly at startup guarantees the conservative batch-size and gzip
    compression defaults apply regardless of which code path first touches
    the OTEL SDK, keeping export requests under the OTLP collector ingress
    body-size limit (nginx 413)."""
    startup = _function_node(APP_DIR / "worker.py", "startup")
    assert _call_lines(startup, "_seed_otel_exporter_limits_from_env"), (
        "worker startup hook must call _seed_otel_exporter_limits_from_env() "
        "before any Langfuse initialization so OTEL BSP batch size and "
        "compression defaults are applied to the worker process"
    )
