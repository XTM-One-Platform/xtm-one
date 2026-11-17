"""The MAX_REQUESTS recycle, never in the middle of an agent turn.

uvicorn's own recycle stopped a worker the moment it reached its quota and cut
every chat turn running in it once GRACEFUL_TIMEOUT had passed, while the pod
stayed up. The launcher hands the recycle to the app instead: past its quota
the worker keeps serving until no agent turn runs in it, at most
MAX_REQUESTS_DEFER_SECONDS, then signals itself, which is uvicorn's own
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import pytest

from app.core import worker_recycle, worker_shutdown
from app.core.worker_recycle import (
    IDLE,
    OVERDUE,
    RecyclePolicy,
    WorkerRecycler,
    log_worker_shutdown_summary,
    policy_from_env,
)
from app.core.worker_shutdown import RECYCLE, STOP


@pytest.fixture(autouse=True)
def _fresh_worker(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(worker_shutdown, "_turns", {})


@pytest.fixture
def busy(monkeypatch) -> list[int]:
    """How many agent turns run in the worker: ``busy[0]``."""
    count = [0]
    monkeypatch.setattr(worker_shutdown, "agent_work_in_flight", lambda: count[0])
    return count


@pytest.fixture
def signals(monkeypatch) -> list[tuple[int, int]]:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_recycle.os, "kill", lambda pid, signum: sent.append((pid, signum)))
    return sent


def _recycler(requests: int = 10, *, defer: int = 60) -> WorkerRecycler:
    return WorkerRecycler(RecyclePolicy(requests=requests, jitter=5, defer_seconds=defer), draw=lambda _a, _b: 2)


# ---------------------------------------------------------------------------
# What the launcher handed over
# ---------------------------------------------------------------------------


def test_a_worker_the_launcher_did_not_start_recycles_nothing():
    assert policy_from_env({}) is None
    assert policy_from_env({worker_recycle.RECYCLE_REQUESTS_ENV: "0"}) is None
    assert policy_from_env({worker_recycle.RECYCLE_REQUESTS_ENV: "lots"}) is None

    recycler = WorkerRecycler(None)
    for _ in range(50):
        recycler.record_request()
    assert recycler.due_at is None and recycler.seen == 50


def test_the_policy_reads_what_the_launcher_validated():
    assert policy_from_env(
        {
            worker_recycle.RECYCLE_REQUESTS_ENV: "10000",
            worker_recycle.RECYCLE_JITTER_ENV: "123",
            worker_recycle.RECYCLE_DEFER_ENV: "600",
        }
    ) == RecyclePolicy(requests=10000, jitter=123, defer_seconds=600)
    assert policy_from_env({worker_recycle.RECYCLE_REQUESTS_ENV: "8000"}) == RecyclePolicy(
        requests=8000, jitter=4000, defer_seconds=worker_recycle.DEFAULT_DEFER_SECONDS
    )


def test_each_worker_draws_its_own_jitter_once():
    draws: list[tuple[int, int]] = []

    def _draw(low: int, high: int) -> int:
        draws.append((low, high))
        return 7

    recycler = WorkerRecycler(RecyclePolicy(requests=100, jitter=50, defer_seconds=0), draw=_draw)

    assert recycler.limit == 107
    assert draws == [(0, 50)]


# ---------------------------------------------------------------------------
# When it recycles
# ---------------------------------------------------------------------------


def test_the_quota_is_logged_once_with_what_the_recycle_waits_for(
    busy, caplog: pytest.LogCaptureFixture
):
    recycler = _recycler(10)
    busy[0] = 2

    with caplog.at_level(logging.WARNING, logger="app.worker_recycle"):
        for _ in range(11):
            recycler.record_request()
        assert recycler.due_at is None
        recycler.record_request()  # the 12th: 10 + its jitter of 2
        for _ in range(5):
            recycler.record_request()

    [record] = caplog.records
    message = record.getMessage()
    assert "served its 12 requests" in message
    assert "once its 2 running agent turn(s) end, at most 60 s from now" in message
    assert "The pod stays up" in message
    assert recycler.due_at is not None


def test_a_worker_below_its_quota_never_recycles(busy):
    recycler = _recycler(10)
    recycler.record_request()

    assert recycler.decision(time.monotonic() + 10**6, 0) is None


def test_a_due_worker_recycles_the_moment_nothing_runs_in_it(busy):
    recycler = _recycler(10)
    for _ in range(12):
        recycler.record_request()
    now = recycler.due_at

    assert recycler.decision(now + 1, 1) is None
    assert recycler.decision(now + 59, 3) is None
    assert recycler.decision(now + 2, 0) == IDLE


def test_a_due_worker_stops_waiting_past_its_deferral(busy):
    recycler = _recycler(10, defer=60)
    for _ in range(12):
        recycler.record_request()

    assert recycler.decision(recycler.due_at + 60, 1) == OVERDUE


def test_a_worker_is_overdue_at_its_deadline_whatever_its_clock_reads(busy):
    recycler = _recycler(10, defer=60)
    for _ in range(12):
        recycler.record_request()
    recycler.due_at = 4078.3649156431497
    assert (recycler.due_at + 60) - recycler.due_at < 60

    assert recycler.decision(recycler.due_at + 60, 1) == OVERDUE


def test_a_zero_deferral_recycles_at_the_quota_as_uvicorn_did(busy):
    recycler = _recycler(10, defer=0)
    for _ in range(12):
        recycler.record_request()

    assert recycler.decision(recycler.due_at, 5) == OVERDUE


def test_the_recycle_records_itself_then_signals_the_worker(busy, signals, caplog: pytest.LogCaptureFixture):
    recycler = _recycler(10)
    for _ in range(12):
        recycler.record_request()

    with caplog.at_level(logging.WARNING, logger="app.worker_recycle"):
        recycler.recycle(IDLE)

    assert worker_shutdown.cut_cause() == RECYCLE
    assert worker_shutdown.shutdown_request().detail == "12 requests"
    assert signals == [(os.getpid(), signal.SIGTERM)]
    assert "recycles now: 12 requests served, no agent turn runs in it" in caplog.text


def test_an_overdue_recycle_names_the_turns_it_cuts(busy, signals, caplog: pytest.LogCaptureFixture):
    worker_shutdown._turns["t"] = worker_shutdown.RunningTurn("conv-1", time.monotonic() - 5000)
    busy[0] = 2
    recycler = _recycler(10)
    for _ in range(12):
        recycler.record_request()

    with caplog.at_level(logging.WARNING, logger="app.worker_recycle"):
        recycler.recycle(OVERDUE)

    assert "outlived MAX_REQUESTS_DEFER_SECONDS (60 s)" in caplog.text
    assert "conversation conv-1 (running 83 min)" in caplog.text
    assert "and 1 background turn(s)" in caplog.text


async def test_the_watcher_waits_for_the_running_turns(busy, signals):
    recycler = _recycler(10)
    for _ in range(12):
        recycler.record_request()
    busy[0] = 1

    watch = asyncio.create_task(recycler.watch(interval=0.01))
    await asyncio.sleep(0.05)
    assert signals == [] and not watch.done()

    busy[0] = 0
    await asyncio.wait_for(watch, 1)
    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_a_worker_handed_no_recycle_starts_no_watcher(monkeypatch):
    monkeypatch.setattr(worker_recycle, "recycler", WorkerRecycler(None))

    assert worker_recycle.start_recycle_watch() is None


# ---------------------------------------------------------------------------
# The exit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "said"),
    [
        (None, "no recycle or stop signal recorded"),
        ((RECYCLE, "12000 requests"), "recycled after 12000 requests (MAX_REQUESTS)"),
        ((STOP, "SIGTERM"), "asked to stop by SIGTERM"),
    ],
    ids=["unknown", "recycle", "stop"],
)
def test_every_exit_says_what_stopped_the_worker(request_, said, caplog: pytest.LogCaptureFixture):
    if request_ is not None:
        worker_shutdown.note_shutdown(*request_)

    with caplog.at_level(logging.WARNING, logger="app.worker_recycle"):
        log_worker_shutdown_summary()

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "shutting down after" in record.getMessage()
    assert said in record.getMessage()
