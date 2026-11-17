"""A run that does not hold a work item never completes it while its holder lives (#8878).

A second dispatch stands down with a note, but its explicit
``complete_work_item`` and its inbox settlement used to close the item while
the holding run was still working it. Both now leave the holder's item alone;
a person, the holder itself, or anyone after the holder ended still completes it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer

AGENT = agent_writer(is_company_managed=False)


def _item(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        workspace_id=None,
        owner_agent_id=uuid.uuid4(),
        created_by_agent_id=None,
        title="Fix the thing",
        description="brief",
        payload={},
        attachment_file_ids=None,
        status="in_progress",
        priority="medium",
        correlation_key="dev:thing",
        due_at=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=uuid.uuid4(),
        claim_notes=None,
        claimed_at=now - timedelta(minutes=5),
        source="agent",
        result=None,
        action_mode=None,
        blocked_on=None,
        fleet_impact=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, one):
        self._one = one

    def scalar_one_or_none(self):
        return self._one


class _Session:
    def __init__(self, item):
        self._item = item
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Result(self._item)

    async def commit(self):
        self.committed = True


@pytest.fixture
def world(monkeypatch):
    """Patch the store around *item*, its holder run reported with *holder_status*."""

    def _make(item, holder_status):
        session = _Session(item)
        monkeypatch.setattr("app.core.database.async_session", lambda: session)

        async def _tiers(db, user_id):
            return [], []

        async def _gate(db, writer, it, action):
            return object()

        async def _holders(db, run_ids):
            return {rid: wi_service.HoldingRun(holder_status, None, "run") for rid in run_ids}

        async def _publish(events):
            return None

        async def _passthrough(work_item_id, user_id=None, **_kw):
            return {"id": str(item.id), "title": item.title, "status": item.status}

        async def _routed(db, it, **_kw):
            return None

        monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
        monkeypatch.setattr(wi_service, "_gate_write", _gate)
        monkeypatch.setattr(wi_service, "holding_runs", _holders)
        monkeypatch.setattr(wi_service, "_publish", _publish)
        monkeypatch.setattr(wi_service, "get_work_item", _passthrough)
        monkeypatch.setattr(wi_service, "update_events", lambda it, **kw: [])
        monkeypatch.setattr(
            "app.services.work_item_fleet_impact.note_impact_routed", _routed
        )
        return session

    return _make


# -- complete_work_item ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_cannot_complete_an_item_a_live_run_of_another_holds(world):
    item = _item()
    held_by = item.claimed_by_run_id
    session = world(item, "running")

    with pytest.raises(ValueError, match=f"held by run {held_by}"):
        await wi_service.complete_work_item(
            str(item.id), user_id=str(item.user_id), result="stood down",
            writer=AGENT, writer_run_ids=(uuid.uuid4(),),
        )
    assert item.status == "in_progress" and item.result is None
    assert not session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "holder_status", "run_ids"),
    [(True, "running", "own"), (False, "completed", "other"), (False, None, "other"), (False, "running", None)],
    ids=["its-own-run", "holder-ended", "holder-purged", "a-person"],
)
async def test_completion_goes_through_for_the_holder_an_ended_holder_or_a_person(
    world, own, holder_status, run_ids
):
    item = _item()
    world(item, holder_status)
    writer_runs = {"own": (item.claimed_by_run_id,), "other": (uuid.uuid4(),), None: None}[run_ids]

    await wi_service.complete_work_item(
        str(item.id), user_id=str(item.user_id), result="landed",
        writer=AGENT, writer_run_ids=writer_runs,
    )
    assert item.status == "done" and item.result == "landed"


# -- settle_inbox_work_item -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_inbox_end_of_a_stand_down_run_keeps_the_holders_item(world):
    item = _item()
    session = world(item, "running")

    out = await wi_service.settle_inbox_work_item(
        str(item.id), user_id=str(item.user_id), result="Stood down.",
        writer=AGENT, run_id=uuid.uuid4(),
    )
    assert out == "kept"
    assert item.status == "in_progress" and item.result is None
    assert not session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_status", ["running", "completed"], ids=["holder-itself", "holder-ended"])
async def test_the_inbox_end_of_the_holder_or_after_it_settles(world, holder_status):
    item = _item()
    world(item, holder_status)
    run_id = item.claimed_by_run_id if holder_status == "running" else uuid.uuid4()

    out = await wi_service.settle_inbox_work_item(
        str(item.id), user_id=str(item.user_id), result="Run summary.",
        writer=AGENT, run_id=run_id,
    )
    assert out == "completed" and item.status == "done"
