"""run.completed / run.failed for a delegated child run (no assignment).

A delegated run (a delegation, a background task, a consult) ends through
``sub_agent.update_assignment_run``, never through the assignment engine: it
raises its ``run.*`` event itself, after the commit, with ``parent_run_id``.
An assignment run is left to the engine (no second event).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import assignment_engine as engine
from app.services import sub_agent


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Db:
    """The AsyncSession surface update_assignment_run uses, with a commit hook."""

    def __init__(self, row: Any) -> None:
        self._row = row
        self.sync_session = object()

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._row)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    listeners: list[Any] = []
    published: list[tuple[Any, str, dict[str, Any]]] = []

    def _listen(_target: Any, name: str, fn: Any, **_kw: Any) -> None:
        assert name == "after_commit"
        listeners.append(fn)

    async def _publish(run: Any, name: str, *, reason: str, error: str | None = None) -> None:
        published.append((run, name, {"reason": reason, "error": error}))

    import sqlalchemy.event

    monkeypatch.setattr(sqlalchemy.event, "listen", _listen)
    monkeypatch.setattr(engine, "publish_run_event", _publish)
    monkeypatch.setattr("app.services.in_process_runs.untrack_in_process_run", lambda _rid: None)
    return {"listeners": listeners, "published": published}


def _run(**extra: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "assignment_id": None,
        "parent_run_id": uuid.uuid4(),
        "trigger_context": {},
        "user_id": uuid.uuid4(),
        "is_company_managed": True,
        "stop_reason": None,
        "status": "running",
        "completed_at": None,
        "result_summary": None,
        "error": None,
        "messages": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


async def _commit(world: dict[str, Any]) -> None:
    for fn in list(world["listeners"]):
        fn(object())
    world["listeners"].clear()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_delegated_run_completed_is_published_after_commit_with_parent(world: dict[str, Any]) -> None:
    run = _run()
    await sub_agent.update_assignment_run(_Db(run), str(run.id), "completed", result_summary="ok")
    # Nothing before the commit: a rolled-back ending raises nothing.
    assert world["published"] == []
    await _commit(world)
    [(snapshot, name, data)] = world["published"]
    assert name == "run.completed" and data["reason"] == "completed"
    assert snapshot.id == run.id and snapshot.parent_run_id == run.parent_run_id
    assert snapshot.assignment_id is None


@pytest.mark.asyncio
async def test_delegated_run_failed_carries_the_error(world: dict[str, Any]) -> None:
    run = _run()
    await sub_agent.update_assignment_run(_Db(run), str(run.id), "failed", error="boom")
    await _commit(world)
    [(_snapshot, name, data)] = world["published"]
    assert name == "run.failed" and data == {"reason": "failed", "error": "boom"}


@pytest.mark.asyncio
async def test_assignment_run_is_left_to_the_engine(world: dict[str, Any]) -> None:
    run = _run(assignment_id=uuid.uuid4())
    await sub_agent.update_assignment_run(_Db(run), str(run.id), "completed")
    await _commit(world)
    assert world["published"] == [] and world["listeners"] == []


@pytest.mark.asyncio
async def test_no_event_for_a_repeated_or_non_terminal_status(world: dict[str, Any]) -> None:
    done = _run(status="completed")
    await sub_agent.update_assignment_run(_Db(done), str(done.id), "completed")
    pending = _run()
    await sub_agent.update_assignment_run(_Db(pending), str(pending.id), "cancelled")
    await _commit(world)
    assert world["published"] == []


@pytest.mark.asyncio
async def test_publish_run_event_scopes_a_delegated_run_to_its_parent_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import platform_events as pe

    parent_agent = uuid.uuid4()

    class _Res:
        def scalar_one_or_none(self) -> Any:
            return parent_agent

        def one_or_none(self) -> Any:
            return None

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def execute(self, _stmt: Any) -> _Res:
            return _Res()

    sent: list[tuple[str, dict[str, Any]]] = []

    async def _send(name: str, data: dict[str, Any]) -> str:
        sent.append((name, data))
        return "e1"

    monkeypatch.setattr(engine, "async_session", _Session)
    monkeypatch.setattr(pe, "publish", _send)
    run = _run()
    await engine.publish_run_event(run, "run.completed", reason="completed")
    [(name, data)] = sent
    assert name == "run.completed"
    assert data["parent_run_id"] == str(run.parent_run_id)
    assert data["assignment_id"] is None
    assert str(parent_agent) in data["participant_agent_ids"]
