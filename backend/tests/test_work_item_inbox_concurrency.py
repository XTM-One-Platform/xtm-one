"""Work-item inbox concurrency: ``trigger_config.max_concurrent_runs``.

A ``work_item`` inbox runs up to N items in parallel, each on its own claimed
item, ordered by priority then due then age; the default stays one at a time.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.autonomous as autonomous_mod
import app.services.work_item as wi_service
from app.services import assignment_dispatch
from app.services.builtin_tools.agent_builder import work_item_trigger_error


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QueueSession:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, *_a, **_k):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _inbox(trigger_config):
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        enabled=True,
        name="Inbox",
        trigger_type="work_item",
        trigger_config=trigger_config,
    )
    agent = SimpleNamespace(id=agent_id, name="Agent", enabled=True, is_company_managed=False)
    return assignment, agent, _QueueSession([_ScalarsResult([assignment]), _ScalarsResult([agent])])


class _Runs:
    """A shared run table: the dispatcher and the trigger guard read the same count."""

    def __init__(self, active=0):
        self.active = active
        self.triggered: list[str] = []
        self.caps: list[int] = []

    async def count(self, _db, _assignment_id):
        return self.active

    async def has_active(self, _db, _assignment_id):
        return self.active > 0

    async def trigger(self, **kwargs):
        self.caps.append(kwargs.get("max_active_runs", 1))
        if self.active >= kwargs.get("max_active_runs", 1):
            return None
        self.active += 1
        self.triggered.append(kwargs["payload"]["work_item_id"])
        return f"run-{len(self.triggered)}"


def _patch(monkeypatch, runs, items, claimed=None, released=None):
    claimed = claimed if claimed is not None else set()

    async def _list(**kwargs):
        return [i for i in items if i["id"] not in claimed][: kwargs.get("limit", 100)]

    async def _claim(work_item_id, **_k):
        if work_item_id in claimed:
            return {"claimed": False}
        claimed.add(work_item_id)
        return {"claimed": True}

    async def _release(*, work_item_id, **_k):
        claimed.discard(work_item_id)
        if released is not None:
            released.append(work_item_id)

    async def _record(*_a, **_k):
        return None

    monkeypatch.setattr(autonomous_mod, "has_active_run", runs.has_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", runs.trigger)
    monkeypatch.setattr("app.services.assignment_engine.count_active_runs", runs.count)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)
    monkeypatch.setattr(wi_service, "release_work_item_claim", _release)
    monkeypatch.setattr(wi_service, "record_claim_run", _record)
    return claimed


def _items(n):
    return [{"id": f"wi-{i}", "correlation_key": None, "title": str(i)} for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_one_pass_fills_every_free_slot(monkeypatch):
    """max_concurrent_runs 3 with 5 open items: one pass starts 3 runs on 3 distinct items."""
    _assignment, _agent, db = _inbox({"max_concurrent_runs": 3})
    runs = _Runs()
    _patch(monkeypatch, runs, _items(5))

    assert await autonomous_mod._process_work_item_assignments(db) == 3
    assert runs.triggered == ["wi-1", "wi-2", "wi-3"]
    assert set(runs.caps) == {3}


@pytest.mark.asyncio
async def test_a_freed_slot_takes_the_next_item(monkeypatch):
    """When a run ends, the next pass (the wake) starts the 4th item, and only it."""
    assignment, agent, db = _inbox({"max_concurrent_runs": 3})
    runs = _Runs()
    claimed = _patch(monkeypatch, runs, _items(5))
    assert await autonomous_mod._process_work_item_assignments(db) == 3

    runs.active -= 1  # one run ended; its item is done (stays claimed)
    db2 = _QueueSession([_ScalarsResult([assignment]), _ScalarsResult([agent])])
    assert await autonomous_mod._process_work_item_assignments(db2) == 1
    assert runs.triggered[-1] == "wi-4"
    assert "wi-5" not in claimed


@pytest.mark.asyncio
async def test_full_inbox_claims_nothing(monkeypatch):
    _assignment, _agent, db = _inbox({"max_concurrent_runs": 2})
    runs = _Runs(active=2)
    claimed = _patch(monkeypatch, runs, _items(3))

    assert await autonomous_mod._process_work_item_assignments(db) == 0
    assert claimed == set()


@pytest.mark.asyncio
async def test_default_stays_serial(monkeypatch):
    """No key: one item per pass, and the guard is the one-run guard."""
    _assignment, _agent, db = _inbox({})
    runs = _Runs()
    _patch(monkeypatch, runs, _items(4))

    assert await autonomous_mod._process_work_item_assignments(db) == 1
    assert runs.caps == [1]


@pytest.mark.asyncio
async def test_racing_pass_never_exceeds_the_cap_nor_double_claims(monkeypatch):
    """Another dispatcher filled the slots between the count and the trigger:
    the guard refuses, the claim goes back, and no item is started twice."""
    _assignment, _agent, db = _inbox({"max_concurrent_runs": 2})
    runs = _Runs()
    released: list[str] = []
    _patch(monkeypatch, runs, _items(4), released=released)

    original = runs.trigger

    async def _raced(**kwargs):
        runs.active = 2  # the sibling pass took both slots
        return await original(**kwargs)

    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _raced)
    assert await autonomous_mod._process_work_item_assignments(db) == 0
    assert released == ["wi-1"]
    assert runs.triggered == []


@pytest.mark.asyncio
async def test_lost_claims_fall_through_to_the_next_items(monkeypatch):
    _assignment, _agent, db = _inbox({"max_concurrent_runs": 2})
    runs = _Runs()
    claimed = {"wi-1"}  # a concurrent claimer won it
    items = _items(4)

    async def _list(**kwargs):
        return items  # the stale pool still lists wi-1

    _patch(monkeypatch, runs, items, claimed=claimed)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list)
    assert await autonomous_mod._process_work_item_assignments(db) == 2
    assert runs.triggered == ["wi-2", "wi-3"]


@pytest.mark.parametrize(
    ("config", "cap"),
    [
        ({}, 1),
        (None, 1),
        ({"max_concurrent_runs": 6}, 6),
        ({"max_concurrent_runs": 50}, 10),
        ({"max_concurrent_runs": 0}, 1),
        ({"max_concurrent_runs": True}, 1),
        ({"max_concurrent_runs": "x"}, 1),
    ],
)
def test_inbox_cap(config, cap):
    assert assignment_dispatch.work_item_inbox_cap(config) == cap


def test_cap_by_trigger_type():
    """A work_item inbox defaults to 1, never to the platform-wide delivery cap."""
    assert assignment_dispatch.concurrency_cap("work_item", {}) == 1
    assert assignment_dispatch.concurrency_cap("webhook", {"max_concurrent_runs": 4}) == 4


@pytest.mark.parametrize("value", [1, 6, 10])
def test_builder_accepts_max_concurrent_runs(value):
    assert work_item_trigger_error("work_item", {"max_concurrent_runs": value}) is None


@pytest.mark.parametrize("value", [0, 11, -1, True, "3", 2.5])
def test_builder_refuses_bad_max_concurrent_runs(value):
    error = work_item_trigger_error("work_item", {"max_concurrent_runs": value})
    assert error is not None and "max_concurrent_runs" in error


def test_rest_create_refuses_bad_max_concurrent_runs():
    from pydantic import ValidationError

    from app.api.v1.schemas.assignment import CreateAssignmentRequest

    with pytest.raises(ValidationError, match="max_concurrent_runs"):
        CreateAssignmentRequest(
            name="Inbox",
            agent_id=str(uuid.uuid4()),
            prompt="Work the item.",
            trigger_type="work_item",
            trigger_config={"max_concurrent_runs": 11},
        )


@pytest.mark.asyncio
async def test_dispatch_order_is_priority_then_due_then_age(monkeypatch):
    from sqlalchemy.dialects import postgresql

    statements = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_e):
            return False

        async def get(self, *_a, **_k):
            return None

        async def execute(self, statement, *_a, **_k):
            statements.append(statement)
            return _ScalarsResult([])

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    from app.services.company_managed_writes import agent_writer

    await wi_service.list_dispatchable_work_items(
        owner_agent_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        writer=agent_writer(is_company_managed=True),
    )
    sql = str(statements[-1].compile(dialect=postgresql.dialect())).lower()
    order = sql.split("order by", 1)[1]
    assert order.index("case") < order.index("due_at") < order.index("created_at")
    assert "desc" in order.split("due_at", 1)[0]
