"""Delegation reserve: a delegated child whose parent run waits for it starts on a full pool.

N parents fill the shared pool and each waits on one delegated child (an assignment child run or a background
sub-agent task): every child must start within one claim cycle, on the reserve above the pool, while ordinary
queued work still waits for a shared slot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.services import orchestration_engine
from app.services import orchestration_reconciler as reconciler
from app.services.orchestration_adapters import assignment as assignment_adapter
from app.services.orchestration_adapters import sub_agent as sub_agent_adapter

_BASE = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> _Result:
        return self


class _Session:
    """Answers the reads of a deal in order."""

    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = list(answers)

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._answers.pop(0))


def _task(parent_run_id: uuid.UUID | None, at: int) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        payload={"task_id": str(uuid.uuid4()), "parent_run_id": str(parent_run_id) if parent_run_id else None},
        run_at=_BASE + timedelta(seconds=at),
    )


@pytest.mark.asyncio
async def test_sub_agent_deal_puts_children_of_live_parents_first() -> None:
    live, ended = uuid.uuid4(), uuid.uuid4()
    chat_task = _task(None, 0)
    orphan = _task(ended, 1)
    child = _task(live, 2)
    session: Any = _Session([[live]])
    ordered = await sub_agent_adapter.SubAgentAdapter().deal_claims(session, [chat_task, orphan, child], "w1")
    assert [e.id for e in ordered] == [child.id, chat_task.id, orphan.id]


@pytest.mark.asyncio
async def test_sub_agent_reserve_deal_keeps_children_of_live_parents_only() -> None:
    live, ended = uuid.uuid4(), uuid.uuid4()
    a, b, c = _task(live, 0), _task(ended, 1), _task(live, 2)
    session: Any = _Session([[live]])
    reserved = await sub_agent_adapter.SubAgentAdapter().deal_reserved_claims(session, [a, b, c], "w1")
    assert [e.id for e in reserved] == [a.id, c.id]


@pytest.mark.asyncio
async def test_sub_agent_deal_reads_nothing_without_a_parent() -> None:
    tasks = [_task(None, at) for at in range(3)]
    session: Any = _Session([])
    assert await sub_agent_adapter.SubAgentAdapter().deal_claims(session, tasks, "w1") == tasks
    assert await sub_agent_adapter.SubAgentAdapter().deal_reserved_claims(session, tasks, "w1") == []


@pytest.mark.asyncio
async def test_assignment_reserve_deal_keeps_delegated_runs_of_live_parents_only() -> None:
    live, ended = uuid.uuid4(), uuid.uuid4()
    runs = [uuid.uuid4() for _ in range(3)]
    executions: Any = [
        SimpleNamespace(id=uuid.uuid4(), payload={"run_id": str(run)}, run_at=_BASE + timedelta(seconds=i))
        for i, run in enumerate(runs)
    ]
    session: Any = _Session([[(runs[0], live), (runs[2], ended)], [live]])
    reserved = await assignment_adapter.AssignmentAdapter().deal_reserved_claims(session, executions, "w1")
    assert [e.id for e in reserved] == [executions[0].id]


def test_reserve_inflight_never_shrinks_the_shared_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = SimpleNamespace(name="sub_agent")
    monkeypatch.setattr(reconciler, "_semaphore_capacity", lambda: 4)
    sentinels: Any = {object() for _ in range(6)}
    reconciler._execution_tasks.update(sentinels)
    reconciler._inflight_by_adapter.clear()
    reconciler._inflight_by_adapter[reconciler.DELEGATION_RESERVE_SLOT] = 2
    try:
        # 6 in flight, 2 of them on the reserve: the shared pool (4) is full, not over-full.
        assert reconciler._available_slots_for(shared, [shared]) == 0
        reconciler._inflight_by_adapter[reconciler.DELEGATION_RESERVE_SLOT] = 3
        assert reconciler._available_slots_for(shared, [shared]) == 1
    finally:
        reconciler._execution_tasks.difference_update(sentinels)
        reconciler._inflight_by_adapter.clear()


def test_reserve_size_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "delegation_reserve_slots", 3)
    reconciler._inflight_by_adapter.clear()
    reconciler._inflight_by_adapter[reconciler.DELEGATION_RESERVE_SLOT] = 1
    try:
        assert reconciler._reserve_slots_free() == 2
        monkeypatch.setattr(settings, "delegation_reserve_slots", 0)
        assert reconciler._reserve_slots_free() == 0
    finally:
        reconciler._inflight_by_adapter.clear()


@pytest.mark.asyncio
async def test_full_pool_of_waiting_parents_starts_every_child_in_one_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """N parents hold the whole pool, each waits on one child: one claim cycle starts every child on the reserve."""
    parents = 4
    monkeypatch.setattr(settings, "delegation_reserve_slots", parents)
    monkeypatch.setattr(reconciler, "_lease_seconds_of", lambda _adapter: None)
    reconciler._inflight_by_adapter.clear()

    children = [_task(uuid.uuid4(), i) for i in range(parents)]
    ordinary = [_task(None, 100 + i) for i in range(3)]
    live = {c.payload["parent_run_id"] for c in children}

    async def _deal(_db: object, due: list[Any], _worker: str) -> list[Any]:
        return [e for e in due if e.payload.get("parent_run_id") in live]

    claims: list[dict[str, Any]] = []

    async def _claim_due(
        _db: object, *, adapter: str, worker_id: str, limit: int, lease_seconds: Any, deal: Any, slot: str
    ) -> list[Any]:
        claims.append({"adapter": adapter, "limit": limit, "slot": slot})
        return list(await deal(_db, [*children, *ordinary], worker_id))[:limit]

    spawned: list[tuple[Any, str, str]] = []

    def _spawn(execution_id: Any, adapter_name: str, worker_id: str, *, slot: str = "") -> None:
        spawned.append((execution_id, adapter_name, slot))
        reconciler._inflight_by_adapter[slot] = reconciler._inflight_by_adapter.get(slot, 0) + 1

    class _Db:
        async def __aenter__(self) -> _Db:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def commit(self) -> None:
            return None

    monkeypatch.setattr(orchestration_engine, "claim_due", _claim_due)
    monkeypatch.setattr(reconciler, "async_session", lambda: _Db())
    monkeypatch.setattr(reconciler, "_spawn_execution_task", _spawn)
    adapter = SimpleNamespace(name="sub_agent", deal_reserved_claims=_deal)
    try:
        taken = await reconciler._claim_into_reserve(adapter, "w1", batch=10)
        assert sorted(taken) == sorted(c.id for c in children)
        assert {slot for _id, _adapter, slot in spawned} == {reconciler.DELEGATION_RESERVE_SLOT}
        assert claims == [{"adapter": "sub_agent", "limit": parents, "slot": reconciler.DELEGATION_RESERVE_SLOT}]
        # The reserve is full now: the ordinary work waits for a shared slot.
        assert await reconciler._claim_into_reserve(adapter, "w1", batch=10) == []
    finally:
        reconciler._inflight_by_adapter.clear()


@pytest.mark.asyncio
async def test_an_adapter_without_a_reserve_deal_claims_nothing_into_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "delegation_reserve_slots", 2)

    async def _never(*_a: object, **_k: object) -> list[Any]:
        raise AssertionError("no claim expected")

    monkeypatch.setattr(orchestration_engine, "claim_due", _never)
    assert await reconciler._claim_into_reserve(SimpleNamespace(name="evaluation"), "w1", batch=10) == []
