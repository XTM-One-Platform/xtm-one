"""A run that completed and left its item open raises no ``run.orphaned``.

Before: the scheduler tick released the claim of a run that had completed on
purpose with its item still open (a red on main still red) and published
``run.orphaned`` with reason "ended completed", which the fleet's orphan
listener counted as a defect on every such run. Now the claim is still
released (``work_item.claim_released``), but ``run.orphaned`` is kept for a
holder that ended without finishing.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.work_item import HoldingRun


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return iter(self._rows)


class _Session:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return self._results.pop(0)


def _item(item_id):
    return SimpleNamespace(
        id=item_id,
        created_by_agent_id=None,
        owner_agent_id=None,
        workspace_id=None,
        user_id=uuid.uuid4(),
        is_company_managed=False,
    )


async def _events(monkeypatch, status):
    run_id, item_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(id=run_id, agent_id=agent_id, assignment_id=uuid.uuid4(), parent_run_id=None)
    session = _Session([_Result([_item(item_id)]), _Result([run]), _Result([])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(
        "app.services.platform_events.work_item_data",
        lambda item, **_kw: {"id": str(item.id), "participant_agent_ids": []},
    )
    monkeypatch.setattr("app.services.platform_events.run_data", lambda **kw: kw)
    holders = {run_id: HoldingRun(status=status, ended_at=None, kind="run")}
    return await wi_service._release_events({run_id: [item_id]}, holders)


@pytest.mark.asyncio
async def test_a_completed_holder_releases_without_an_orphan_event(monkeypatch):
    names = [name for name, _ in await _events(monkeypatch, "completed")]
    assert names == ["work_item.claim_released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", None])
async def test_a_holder_that_did_not_finish_is_still_orphaned(monkeypatch, status):
    events = await _events(monkeypatch, status)
    names = [name for name, _ in events]
    assert names == ["work_item.claim_released", "run.orphaned"]
    reason = dict(events)["run.orphaned"]["reason"]
    assert reason == (f"ended {status}" if status else "no longer recorded")
