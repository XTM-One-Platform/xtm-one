"""``delegate_to_assignment`` refuses a task naming a work item a live run of the same agent holds.

Gap work-item-double-dispatch: a delegation to an agent's pick-up assignment
while that agent's own run holds the item started a second run that could only
stand down. ``_held_work_item_named`` finds such an item in the task text.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import work_item as wi_service
from app.services.builtin_tools import work_items as wi_tools

AGENT = uuid.uuid4()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Answers the one item read with the rows whose id the task names and the agent owns."""

    def __init__(self, items):
        self.items = items
        self.reads = 0

    async def execute(self, _statement):
        self.reads += 1
        return _Rows(self.items)


def _item(*, status="in_progress", run=None, owner=AGENT):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        owner_agent_id=owner,
        claimed_by_run_id=run,
    )


def _holders(monkeypatch, alive: dict[uuid.UUID, bool]):
    async def fake(_db, run_ids):
        return {
            rid: wi_service.HoldingRun(status="running" if alive.get(rid) else "completed", ended_at=None, kind="run")
            for rid in run_ids
        }

    monkeypatch.setattr(wi_service, "holding_runs", fake)


@pytest.mark.asyncio
async def test_a_task_naming_an_item_held_by_a_live_run_is_refused(monkeypatch):
    run = uuid.uuid4()
    item = _item(run=run)
    _holders(monkeypatch, {run: True})
    held = await wi_tools._held_work_item_named(_Db([item]), f"Work the item {item.id} now", AGENT)
    assert held == (str(item.id), str(run))


@pytest.mark.asyncio
async def test_an_item_whose_holder_ended_is_not_refused(monkeypatch):
    run = uuid.uuid4()
    item = _item(run=run)
    _holders(monkeypatch, {run: False})
    assert await wi_tools._held_work_item_named(_Db([item]), f"item {item.id}", AGENT) is None


@pytest.mark.asyncio
async def test_an_open_unheld_item_is_not_refused(monkeypatch):
    item = _item(status="open", run=None)
    _holders(monkeypatch, {})
    assert await wi_tools._held_work_item_named(_Db([item]), f"item {item.id}", AGENT) is None


@pytest.mark.asyncio
async def test_a_task_without_an_id_reads_nothing():
    db = _Db([])
    assert await wi_tools._held_work_item_named(db, "summarise the week", AGENT) is None
    assert db.reads == 0
