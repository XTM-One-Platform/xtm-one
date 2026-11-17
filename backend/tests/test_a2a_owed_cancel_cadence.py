"""The owed-cancel re-send keeps its own cadence.

The documented bound - an owed cancel reaches its partner at most one
delegation ceiling plus one ``a2a_client_sweep_interval_seconds`` after the
decision - only holds if the re-send is checked once per interval whatever
else the sweep does. It used to run first in the card sweep's cycle, so the
next check came one interval PLUS the whole cycle's network work later.

These tests run the real loops with a sub-second interval and stubbed work.
Without the dedicated loop they fall back to the card sweep loop, which is
where the re-send used to live, so they measure the old behaviour too.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools

import pytest

INTERVAL = 0.25


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _loops(main) -> list:
    owed = getattr(main, "_a2a_owed_cancel_loop", None)
    return [main._a2a_client_sweep_loop()] + ([owed()] if owed else [])


async def _run_for(monkeypatch, seconds, *, resend_takes=0.0, sweep_takes=0.0):
    """Run the loops for *seconds*; return the start time of every re-send."""
    from app import main
    from app.core.config import settings

    loop = asyncio.get_running_loop()
    starts: list[float] = []

    async def _reissue(_db, **_kw):
        starts.append(loop.time())
        await asyncio.sleep(resend_takes)
        return 0

    async def _sweep_once(_db, **_kw):
        await asyncio.sleep(sweep_takes)
        return 0

    async def _collect(_db, **_kw):
        return 0

    monkeypatch.setattr(settings, "a2a_client_sweep_interval_seconds", INTERVAL)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr("app.a2a.client.sweep.reissue_owed_cancels", _reissue)
    monkeypatch.setattr("app.a2a.client.sweep.sweep_once", _sweep_once)
    monkeypatch.setattr("app.a2a.client.sweep.collect_overdue", _collect)

    tasks = [asyncio.create_task(c) for c in _loops(main)]
    try:
        await asyncio.sleep(seconds)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
    return starts


async def test_a_slow_card_sweep_no_longer_delays_the_re_send(monkeypatch):
    """Card re-verification stuck on unresponsive partners must not hold
    back the next re-send: it used to wait for the whole cycle."""
    starts = await _run_for(monkeypatch, 4 * INTERVAL + 0.1, sweep_takes=60)
    assert len(starts) >= 4, (
        f"{len(starts)} re-send check(s) in {4 * INTERVAL + 0.1:.2f} s at a "
        f"{INTERVAL} s interval: the card sweep's duration delayed it"
    )


async def test_the_re_send_s_own_duration_does_not_push_the_next_check(monkeypatch):
    """Each cycle starts one interval after the previous one STARTED."""
    starts = await _run_for(monkeypatch, 3 * INTERVAL + 0.1, resend_takes=0.12)
    assert len(starts) >= 3
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(gap < INTERVAL + 0.08 for gap in gaps), (
        f"gaps {gaps}: each cycle waited a full interval after its own work"
    )


def test_startup_starts_the_re_send_loop_and_shutdown_stops_it():
    from app import main

    startup = inspect.getsource(main._do_all_startup_work)
    assert "_a2a_owed_cancel_task = asyncio.create_task(_a2a_owed_cancel_loop())" in startup
    shutdown = inspect.getsource(main.lifespan)
    assert "_a2a_owed_cancel_task.cancel()" in shutdown


def test_the_card_sweep_no_longer_runs_the_re_send():
    """One owner for the re-send: run from both loops it would be checked
    twice per interval, doubling the query the common case costs."""
    from app import main

    assert "reissue_owed_cancels" not in inspect.getsource(main._a2a_client_sweep_loop)
