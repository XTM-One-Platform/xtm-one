"""A finished background task wakes the work item its spawning run was dispatched for (#2215).

21 Nov 2026: a background task finished at 13:42Z; the item whose run had
spawned it stayed open, parked by its ended run, until a person asked hours
later. ``background_task.*`` events now name the spawning run's work item and
the platform notes the task's end on it, reopening it for its owner's inbox
when nothing alive holds it.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services import platform_events as pe
from app.services import sub_agent


def test_the_task_event_names_the_spawning_work_item_and_filters_on_it():
    item = uuid.uuid4()
    data = pe.background_task_data(task_id=uuid.uuid4(), agent_id=uuid.uuid4(), work_item_id=item)
    assert data["work_item_id"] == str(item)
    for name in ("background_task.completed", "background_task.failed"):
        assert "work_item_id" in pe.PLATFORM_EVENTS[name]["filter_keys"]
    assert pe.background_task_data(task_id=uuid.uuid4(), agent_id=uuid.uuid4())["work_item_id"] is None


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _Session:
    def __init__(self, item, alive=False):
        self.item = item
        self.alive = alive
        self.committed = False
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        self.calls += 1
        return _Scalar(self.item if self.calls == 1 else self.alive)

    async def commit(self):
        self.committed = True


def _item(**fields):
    base = dict(
        id=uuid.uuid4(),
        status="in_progress",
        claimed_by_run_id=uuid.uuid4(),
        claimed_by_agent_id=uuid.uuid4(),
        claimed_at=None,
        claim_notes=None,
        blocked_on=None,
        blocked_on_refs=None,
        needs_human=False,
        blocked_reason=None,
        ask=None,
        action_mode=None,
        last_activity_at=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def _capture_transitions(monkeypatch):
    transitions = []

    async def _transition(work_item_id, *, previous_status, note=None, **_kw):
        transitions.append((work_item_id, previous_status, note))

    monkeypatch.setattr(wi_service, "publish_transition", _transition)
    return transitions


@pytest.mark.asyncio
async def test_an_item_left_in_progress_by_an_ended_run_is_reopened(monkeypatch):
    item = _item()
    session = _Session(item, alive=False)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    transitions = _capture_transitions(monkeypatch)
    task = uuid.uuid4()

    assert await wi_service.wake_on_background_task(item.id, task_id=task, outcome="completed") == "reopened"
    assert item.status == "open" and item.claimed_by_run_id is None and item.claimed_by_agent_id is None
    assert item.claim_notes[-1]["text"] == f"background task {task} completed"
    assert session.committed
    # The reopen is published (work_item.status_changed) and nudges the dispatcher, as the other reopens.
    assert transitions == [(item.id, "in_progress", f"background task {task} completed")]


@pytest.mark.asyncio
async def test_an_item_its_live_run_still_holds_is_only_noted(monkeypatch):
    holder = uuid.uuid4()
    item = _item(claimed_by_run_id=holder)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item, alive=True))
    transitions = _capture_transitions(monkeypatch)

    assert await wi_service.wake_on_background_task(item.id, task_id=uuid.uuid4(), outcome="failed") == "noted"
    assert transitions == []
    assert item.status == "in_progress" and item.claimed_by_run_id == holder
    assert item.claim_notes[-1]["text"].endswith(" failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"status": "blocked", "blocked_on": [str(uuid.uuid4())]},
        {"status": "blocked", "needs_human": True},
        {"status": "done"},
        {"status": "in_progress", "action_mode": "exact"},
    ],
)
async def test_an_item_waiting_on_something_else_or_finished_keeps_its_status(monkeypatch, fields):
    item = _item(**fields)
    status = item.status
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item, alive=False))

    assert await wi_service.wake_on_background_task(item.id, task_id=uuid.uuid4(), outcome="completed") == "noted"
    assert item.status == status


@pytest.mark.asyncio
async def test_a_blocked_item_with_no_blocker_is_reopened(monkeypatch):
    item = _item(status="blocked", claimed_by_run_id=None, blocked_reason="waits for the task", ask={"question": "q"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item))
    transitions = _capture_transitions(monkeypatch)

    assert await wi_service.wake_on_background_task(item.id, task_id=uuid.uuid4(), outcome="completed") == "reopened"
    assert item.status == "open"
    # Leaving blocked clears the why fields, as the sibling reopens do.
    assert item.blocked_reason is None and item.ask is None
    assert [previous for _id, previous, _note in transitions] == ["blocked"]


@pytest.mark.asyncio
async def test_no_item_id_or_unknown_item_writes_nothing(monkeypatch):
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(None))
    assert await wi_service.wake_on_background_task(None, task_id="t", outcome="completed") is None
    assert await wi_service.wake_on_background_task(uuid.uuid4(), task_id="t", outcome="completed") is None


@pytest.mark.asyncio
async def test_the_task_event_carries_and_wakes_the_spawning_item(monkeypatch):
    task_id, parent_run, item_id = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    task = SimpleNamespace(
        id=task_id,
        target_agent_id=uuid.uuid4(),
        parent_agent_id=uuid.uuid4(),
        metadata_json={"parent_run_id": str(parent_run)},
        result="done",
        error=None,
    )

    class _TaskSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return task

    published, woken = [], []

    async def _spawning(run_id):
        assert run_id == str(parent_run)
        return item_id

    async def _publish(name, data, **_kw):
        published.append((name, data))

    async def _wake(work_item_id, *, task_id, outcome):
        woken.append((work_item_id, task_id, outcome))

    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession())
    monkeypatch.setattr(sub_agent, "_spawning_work_item", _spawning)
    monkeypatch.setattr(pe, "publish", _publish)
    monkeypatch.setattr(wi_service, "wake_on_background_task", _wake)

    await sub_agent._publish_platform_task_event(str(task_id), "completed")

    assert published[0][0] == "background_task.completed" and published[0][1]["work_item_id"] == item_id
    assert woken == [(item_id, task_id, "completed")]


def test_only_an_inbox_dispatched_parent_run_names_a_work_item():
    from app.services.assignment_engine import dispatched_work_item_id

    item = str(uuid.uuid4())
    assert (
        dispatched_work_item_id(
            {"type": "work_item", "work_item_id": item}, {"trigger_type": "work_item", "work_item_id": item}
        )
        == item
    )
    assert dispatched_work_item_id({"type": "manual"}, {"work_item_id": item}) is None
