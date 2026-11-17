"""The reconciler must self-heal IN-PROCESS, not rely on pod restarts.

Behaviour pinned here:

* A transient failure streak that recovers keeps the liveness heartbeat fresh
  the whole time (no restart churn) and resets on success.
* While failing, the reconciler periodically disposes the DB connection pool to
  rebuild fresh connections in-process (DB failover / half-open sockets) — no
  pod restart needed.
* Only a SUSTAINED, unrecoverable failure streak (longer than
  ``reconciler_unhealthy_restart_seconds``) stops refreshing the heartbeat, so
  the livenessProbe restarts the worker as a true last resort.
"""

from __future__ import annotations

import asyncio

from app.services import orchestration_reconciler as rec


async def _no_pubsub():
    return None, None


def test_transient_failures_keep_heartbeat_fresh_then_recover(monkeypatch):
    monkeypatch.setattr(rec.settings, "reconciler_unhealthy_restart_seconds", 600)
    monkeypatch.setattr(rec.settings, "reconciler_pool_recovery_after_failures", 3)

    heartbeats: list[int] = []
    recoveries: list[int] = []
    state = {"i": 0}

    async def _run_cycle(_worker_id):
        state["i"] += 1
        if state["i"] in (1, 2):
            raise RuntimeError("transient db blip")
        return None

    async def _sleep(_pubsub):
        if state["i"] >= 3:
            rec.stop_reconciler()

    async def _recover():
        recoveries.append(1)

    monkeypatch.setattr(rec, "register_builtin_adapters", lambda: None)
    monkeypatch.setattr(rec, "_subscribe_wake", _no_pubsub)
    monkeypatch.setattr(rec, "_run_cycle", _run_cycle)
    monkeypatch.setattr(rec, "_sleep_or_wake", _sleep)
    monkeypatch.setattr(rec, "_recover_db_pool", _recover)
    monkeypatch.setattr(rec, "_write_heartbeat", lambda: heartbeats.append(state["i"]))

    asyncio.run(rec.reconciler_loop())

    # Boot (0) + both failing cycles (1, 2) + the recovering cycle (3): the
    # heartbeat stays fresh throughout, so a transient outage never restarts.
    assert heartbeats == [0, 1, 2, 3]
    # Only two consecutive failures (< 3) → no in-process pool dispose needed.
    assert recoveries == []


def test_sustained_failure_recovers_pool_and_finally_goes_stale(monkeypatch):
    # unhealthy_restart_seconds=0 → every failure immediately counts as
    # "sustained", so the heartbeat is left stale (last-resort restart) while
    # the pool is disposed periodically to attempt in-process recovery.
    monkeypatch.setattr(rec.settings, "reconciler_unhealthy_restart_seconds", 0)
    monkeypatch.setattr(rec.settings, "reconciler_pool_recovery_after_failures", 2)

    heartbeats: list[int] = []
    recoveries: list[int] = []
    state = {"i": 0}

    async def _run_cycle(_worker_id):
        state["i"] += 1
        raise RuntimeError("db down")

    async def _sleep(_pubsub):
        if state["i"] >= 4:
            rec.stop_reconciler()

    async def _recover():
        recoveries.append(state["i"])

    monkeypatch.setattr(rec, "register_builtin_adapters", lambda: None)
    monkeypatch.setattr(rec, "_subscribe_wake", _no_pubsub)
    monkeypatch.setattr(rec, "_run_cycle", _run_cycle)
    monkeypatch.setattr(rec, "_sleep_or_wake", _sleep)
    monkeypatch.setattr(rec, "_recover_db_pool", _recover)
    monkeypatch.setattr(rec, "_write_heartbeat", lambda: heartbeats.append(state["i"]))

    asyncio.run(rec.reconciler_loop())

    # Only the boot heartbeat — every failing cycle is "sustained" so none
    # refresh it, letting the probe restart as a last resort.
    assert heartbeats == [0]
    # Pool recovery attempted every 2 failures → at cycles 2 and 4.
    assert recoveries == [2, 4]
    assert state["i"] >= 4


def test_unhealthy_restart_transition_logged_once_per_streak(monkeypatch, caplog):
    # threshold=0 -> every failing cycle is immediately "sustained", so the
    # heartbeat is never refreshed. The "leaving the heartbeat stale" transition
    # must be logged exactly ONCE across the streak, not on every failing cycle:
    # the per-cycle "Reconciler cycle error" already carries the streak +
    # duration (with traceback) each time, so repeating this would only double
    # the outage log volume without adding signal.
    monkeypatch.setattr(rec.settings, "reconciler_unhealthy_restart_seconds", 0)
    # Keep the pool-recovery cadence out of the way (no dispose in this test).
    monkeypatch.setattr(rec.settings, "reconciler_pool_recovery_after_failures", 1000)

    state = {"i": 0}

    async def _run_cycle(_worker_id):
        state["i"] += 1
        raise RuntimeError("db down")

    async def _sleep(_pubsub):
        if state["i"] >= 5:
            rec.stop_reconciler()

    async def _recover():
        pass

    monkeypatch.setattr(rec, "register_builtin_adapters", lambda: None)
    monkeypatch.setattr(rec, "_subscribe_wake", _no_pubsub)
    monkeypatch.setattr(rec, "_run_cycle", _run_cycle)
    monkeypatch.setattr(rec, "_sleep_or_wake", _sleep)
    monkeypatch.setattr(rec, "_recover_db_pool", _recover)
    monkeypatch.setattr(rec, "_write_heartbeat", lambda: None)

    caplog.set_level("ERROR", logger="app.services.orchestration_reconciler")
    asyncio.run(rec.reconciler_loop())

    # Five failing cycles, but the stale-heartbeat transition logs exactly once.
    assert state["i"] >= 5
    assert caplog.text.count("leaving the heartbeat stale") == 1


def test_recover_db_pool_disposes_engine(monkeypatch):
    import app.core.database as dbmod

    disposed: list[int] = []

    class _FakeEngine:
        async def dispose(self):
            disposed.append(1)

    monkeypatch.setattr(dbmod, "engine", _FakeEngine())

    asyncio.run(rec._recover_db_pool())

    assert disposed == [1]


def test_recover_db_pool_never_raises_on_dispose_failure(monkeypatch):
    import app.core.database as dbmod

    class _FakeEngine:
        async def dispose(self):
            raise RuntimeError("cannot reach db to close connections")

    monkeypatch.setattr(dbmod, "engine", _FakeEngine())

    # A failing/timing-out dispose must be swallowed so recovery can't crash
    # the loop — the next cycle simply retries against the existing pool.
    asyncio.run(rec._recover_db_pool())
