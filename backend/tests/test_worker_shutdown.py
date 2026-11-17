"""Why a web worker stops, and the agent turns it runs when it does.

A turn uvicorn's drain cuts cannot tell from its cancellation whether its
worker recycled itself or was asked to stop by its host: the worker records
which, a stop signal before uvicorn's own handler sees it, and counts the turns
that hold a conversation's lock in it so the recycle can wait for them and the
stop can name them.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

import pytest

from app.core import shutdown_finalizers, worker_shutdown
from app.core.worker_shutdown import (
    RECYCLE,
    STOP,
    RunningTurn,
    ShutdownRequest,
    agent_work_in_flight,
    background_grace_deadline,
    cut_cause,
    describe_turns,
    graceful_timeout,
    install_stop_signal_hook,
    log_stop_requested,
    note_shutdown,
    running_turns,
)
from app.services import chat_turn_guard
from tests.test_chat_web_steering import _FakeRedisKV

CONV = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _fresh_worker(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(worker_shutdown, "_turns", {})
    monkeypatch.setattr(shutdown_finalizers, "_background_turns", set())
    monkeypatch.delenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, raising=False)


@pytest.fixture
def fake_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeRedisKV()

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return fake


# ---------------------------------------------------------------------------
# What stopped the worker
# ---------------------------------------------------------------------------


def test_nothing_recorded_means_no_cause():
    assert cut_cause() is None
    assert background_grace_deadline() is None


def test_the_first_request_wins():
    """A recycle signals itself: the SIGTERM it sends is not a stop from outside."""
    note_shutdown(RECYCLE, "10342 requests")
    note_shutdown(STOP, "SIGTERM")

    assert cut_cause() == RECYCLE
    assert worker_shutdown.shutdown_request().detail == "10342 requests"


def test_the_graceful_timeout_is_read_from_the_launcher(monkeypatch):
    assert graceful_timeout({}) is None
    assert graceful_timeout({worker_shutdown.GRACEFUL_TIMEOUT_ENV: "2m"}) is None
    assert graceful_timeout({worker_shutdown.GRACEFUL_TIMEOUT_ENV: "120"}) == 120.0


def test_the_background_grace_ends_with_the_requests_drain(monkeypatch):
    monkeypatch.setenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, "120")
    request = note_shutdown(STOP, "SIGTERM")

    assert background_grace_deadline() == request.requested_at + 120


# ---------------------------------------------------------------------------
# The turns the worker runs
# ---------------------------------------------------------------------------


async def test_a_turn_counts_from_its_lock_to_its_release(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)

    assert [turn.conversation_id for turn in running_turns()] == [CONV]
    assert agent_work_in_flight() == 1
    assert await chat_turn_guard.acquire_turn(CONV) is None
    assert agent_work_in_flight() == 1

    await chat_turn_guard.release_turn(CONV, token)
    assert running_turns() == []
    assert agent_work_in_flight() == 0


async def test_a_turn_whose_lock_lapsed_still_stops_counting_at_its_release(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    fake_redis.kv.clear()

    assert await chat_turn_guard.release_turn(CONV, token) is False
    assert running_turns() == []


async def test_background_turns_count_too():
    task = asyncio.create_task(asyncio.Event().wait())
    shutdown_finalizers.track_background_turn(task)

    assert agent_work_in_flight() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent_work_in_flight() == 0


def test_turns_are_described_oldest_first_and_summed_past_ten():
    now = 10_000.0
    turns = [RunningTurn(f"c{i}", now - 60 * i) for i in range(12)]

    text = describe_turns(sorted(turns, key=lambda turn: turn.started_at), now=now)

    assert text.startswith("conversation c11 (running 11 min), conversation c10 (running 10 min)")
    assert text.endswith("and 2 more")
    assert describe_turns([], now=now) == "none"
    assert describe_turns([RunningTurn("x", now - 30)], now=now) == "conversation x (running 30 s)"
    assert describe_turns([RunningTurn("x", now - 3 * 3600)], now=now) == "conversation x (running 3.0 h)"


# ---------------------------------------------------------------------------
# The stop signal
# ---------------------------------------------------------------------------


async def test_the_hook_records_the_stop_then_hands_the_signal_to_uvicorn(monkeypatch):
    handlers: dict[int, object] = {}
    received: list[int] = []

    def _uvicorn_handler(signum, _frame):
        received.append(signum)

    monkeypatch.setattr(signal, "getsignal", lambda signum: handlers.get(signum, _uvicorn_handler))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    logged: list[str] = []
    monkeypatch.setattr(worker_shutdown, "log_stop_requested", lambda: logged.append("stop"))

    install_stop_signal_hook()
    install_stop_signal_hook()  # a second install chains nothing more
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    await asyncio.sleep(0)

    assert received == [signal.SIGTERM]
    assert cut_cause() == STOP
    assert worker_shutdown.shutdown_request().detail == "SIGTERM"
    assert logged == ["stop"]


async def test_the_hook_leaves_a_signal_python_does_not_handle(monkeypatch):
    installed: list[int] = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda signum, _handler: installed.append(signum))

    install_stop_signal_hook()

    assert installed == []


async def test_the_recycles_own_sigterm_is_not_a_stop(monkeypatch):
    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "getsignal", lambda signum: handlers.get(signum, lambda *_: None))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    install_stop_signal_hook()

    note_shutdown(RECYCLE, "12000 requests")
    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert cut_cause() == RECYCLE


def test_the_stop_names_the_turns_it_is_about_to_cut(monkeypatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, "120")
    worker_shutdown._turns["t"] = RunningTurn(CONV, time.monotonic() - 3120)
    monkeypatch.setattr(
        worker_shutdown, "_request", ShutdownRequest(STOP, "SIGTERM", time.monotonic())
    )

    with caplog.at_level(logging.WARNING, logger="app.core.worker_shutdown"):
        log_stop_requested()

    message = caplog.records[0].getMessage()
    assert "asked to stop by SIGTERM" in message
    assert f"conversation {CONV} (running 52 min)" in message
    assert "120 s (GRACEFUL_TIMEOUT)" in message
    assert "terminationGracePeriodSeconds must exceed 135 s" in message


def test_a_stop_with_nothing_running_says_so(monkeypatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setattr(
        worker_shutdown, "_request", ShutdownRequest(STOP, "SIGINT", time.monotonic())
    )

    with caplog.at_level(logging.WARNING, logger="app.core.worker_shutdown"):
        log_stop_requested()

    assert "no agent turn is running in it" in caplog.text
