# SPDX-License-Identifier: Apache-2.0

"""An external A2A run is ended by the shutdown drain, not abandoned to the lease.

A2A runs execute detached from the request that created them
(``a2a_server._spawn_task_run``). Outside the shutdown drain's registries, a
deployment killed the process under them with no write: their supervisor
died with them, the lease went stale and the reaper failed the task a full
lease (15 minutes) later with "External task abandoned - execution lease
expired", no reason a partner can act on (gap a2a-long-task-lease-expired).
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.v1 import a2a_server
from app.core import shutdown_finalizers
from app.core.shutdown_finalizers import (
    BACKGROUND_TURN_CANCEL_MESSAGE,
    background_turns_in_flight,
    drain_shutdown_finalizers,
)


@pytest.fixture(autouse=True)
def _empty_registries(monkeypatch):
    monkeypatch.setattr(shutdown_finalizers, "_request_tasks", set())
    monkeypatch.setattr(shutdown_finalizers, "_finalizers", set())
    monkeypatch.setattr(shutdown_finalizers, "_background_turns", set())
    monkeypatch.setattr(shutdown_finalizers, "_before_cancel", {})
    monkeypatch.setattr(a2a_server, "_BACKGROUND_RUNS", set())


async def test_a_spawned_a2a_run_is_cancelled_by_the_shutdown_drain(monkeypatch):
    from app.a2a import tasks as task_svc

    started = asyncio.Event()
    seen: list[str] = []

    async def _run(task_id) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError as exc:
            seen.append(exc.args[0] if exc.args else "")
            raise

    monkeypatch.setattr(task_svc, "execute_task_run", _run)

    a2a_server._spawn_task_run("task-1")
    await started.wait()
    assert background_turns_in_flight() == 1

    await asyncio.wait_for(drain_shutdown_finalizers(timeout=1.0, background_grace_until=None), 2.0)

    assert seen == [BACKGROUND_TURN_CANCEL_MESSAGE]
    assert background_turns_in_flight() == 0
    assert not a2a_server._BACKGROUND_RUNS


async def test_a_finished_a2a_run_leaves_no_turn_behind(monkeypatch):
    from app.a2a import tasks as task_svc

    async def _run(task_id) -> None:
        return None

    monkeypatch.setattr(task_svc, "execute_task_run", _run)

    a2a_server._spawn_task_run("task-2")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert background_turns_in_flight() == 0
    assert not a2a_server._BACKGROUND_RUNS
