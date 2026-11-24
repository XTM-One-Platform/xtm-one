"""A fleet's opt-out of the seeded weekly health check (#114).

Unit tests on session doubles: a fleet that is off or on its own check gets no
seeded check, a deletion of the seeded check records the opt-out, and the
fleet can name one of its own assignments or go back to the default.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.fleets import (
    HEALTH_CHECK_DEFAULT,
    HEALTH_CHECK_OFF,
    HEALTH_CHECK_OWN,
    FleetActionRefusedError,
    ensure_fleet_health_check,
    fleet_health_check_mode,
    record_health_check_deleted,
    set_fleet_health_check,
)


class _Db:
    """A session double: select answers the rows of the next queued answer, get reads a map."""

    def __init__(self, rows=None, objects=None, member_ids=None):
        self.rows = list(rows or [])
        self.objects = dict(objects or {})
        self.member_ids = list(member_ids or [])
        self.deleted = []
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        text = str(stmt)
        if text.lstrip().upper().startswith("UPDATE"):
            return SimpleNamespace()
        if "fleet_members" in text:
            ids = list(self.member_ids)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ids))
        rows = list(self.rows)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None, all=lambda: rows)
        )

    async def get(self, _model, key):
        return self.objects.get(key)

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)
        self.deleted.append(row)


def _fleet():
    return SimpleNamespace(id=uuid.uuid4(), name="Desk", orchestrator_agent_id=None, health_check=None)


def _agent():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)


def test_the_mode_reads_the_default_unless_the_fleet_opted_out():
    assert fleet_health_check_mode(_fleet()) == HEALTH_CHECK_DEFAULT
    off = _fleet()
    off.health_check = {"mode": "off"}
    assert fleet_health_check_mode(off) == HEALTH_CHECK_OFF
    own = _fleet()
    own.health_check = {"mode": "own", "assignment_id": str(uuid.uuid4())}
    assert fleet_health_check_mode(own) == HEALTH_CHECK_OWN
    odd = _fleet()
    odd.health_check = {"mode": "something"}
    assert fleet_health_check_mode(odd) == HEALTH_CHECK_DEFAULT


@pytest.mark.asyncio
async def test_a_fleet_that_opted_out_gets_no_seeded_check():
    for mode in ("off", "own"):
        fleet, agent, db = _fleet(), _agent(), _Db()
        fleet.health_check = {"mode": mode}
        assert not await ensure_fleet_health_check(db, fleet, agent)
        assert db.rows == []


@pytest.mark.asyncio
async def test_a_new_fleet_still_gets_the_seeded_check():
    fleet, agent, db = _fleet(), _agent(), _Db()
    assert await ensure_fleet_health_check(db, fleet, agent)
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_deleting_the_seeded_check_records_the_opt_out_of_its_fleet():
    agent = _agent()
    fleet = _fleet()
    fleet.orchestrator_agent_id = agent.id
    db = _Db()
    await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    db.objects[fleet.id] = fleet
    assert await record_health_check_deleted(db, check, actor="person")
    assert fleet.health_check["mode"] == "off" and fleet.health_check["by"] == "person"
    # Already off: a second deletion keeps the first record.
    first = dict(fleet.health_check)
    assert not await record_health_check_deleted(db, check, actor="other")
    assert fleet.health_check == first


@pytest.mark.asyncio
async def test_deleting_any_other_assignment_records_nothing():
    fleet = _fleet()
    db = _Db(objects={fleet.id: fleet})
    other = SimpleNamespace(name="Weekly fleet health check", activation_check=None, agent_id=uuid.uuid4())
    assert not await record_health_check_deleted(db, other, actor="person")
    plain = SimpleNamespace(name="Something", activation_check={"tool": "x"}, agent_id=uuid.uuid4())
    assert not await record_health_check_deleted(db, plain, actor="person")
    assert fleet.health_check is None


@pytest.mark.asyncio
async def test_a_check_left_on_a_former_orchestrator_records_nothing():
    agent, former = _agent(), _agent()
    fleet = _fleet()
    fleet.orchestrator_agent_id = agent.id
    db = _Db()
    await ensure_fleet_health_check(db, fleet, former)
    (check,) = db.rows
    db.objects[fleet.id] = fleet
    assert not await record_health_check_deleted(db, check, actor="person")
    assert fleet.health_check is None


@pytest.mark.asyncio
async def test_off_removes_the_seeded_check_and_default_brings_it_back():
    agent = _agent()
    fleet = _fleet()
    fleet.orchestrator_agent_id = agent.id
    db = _Db(objects={agent.id: agent})
    await ensure_fleet_health_check(db, fleet, agent)
    record = await set_fleet_health_check(db, fleet, "off", actor="person")
    assert record["mode"] == "off" and fleet.health_check == record
    assert db.rows == [] and len(db.deleted) == 1
    assert await set_fleet_health_check(db, fleet, "default", actor="person") is None
    # The check comes back with its answer listener (#1406) and its overdue items listener (#1662).
    assert fleet.health_check is None and [r.name for r in db.rows] == [
        "Weekly fleet health check",
        "Fleet health answered",
        "Overdue work items",
    ]


@pytest.mark.asyncio
async def test_a_fleet_names_an_assignment_of_a_home_member_as_its_check():
    agent, member = _agent(), _agent()
    fleet = _fleet()
    fleet.orchestrator_agent_id = agent.id
    own = SimpleNamespace(id=uuid.uuid4(), agent_id=member.id, name="Our sweep", activation_check=None)
    db = _Db(objects={agent.id: agent, own.id: own}, member_ids=[agent.id, member.id])
    await ensure_fleet_health_check(db, fleet, agent)
    record = await set_fleet_health_check(db, fleet, str(own.id), actor="person")
    assert record["mode"] == "own" and record["assignment_id"] == str(own.id)
    assert db.rows == []


@pytest.mark.asyncio
async def test_an_assignment_outside_the_fleet_or_a_bad_value_is_refused():
    agent, stranger = _agent(), _agent()
    fleet = _fleet()
    fleet.orchestrator_agent_id = agent.id
    foreign = SimpleNamespace(id=uuid.uuid4(), agent_id=stranger.id, name="Theirs", activation_check=None)
    db = _Db(objects={foreign.id: foreign}, member_ids=[agent.id])
    with pytest.raises(FleetActionRefusedError):
        await set_fleet_health_check(db, fleet, str(foreign.id), actor="person")
    with pytest.raises(FleetActionRefusedError):
        await set_fleet_health_check(db, fleet, "sometimes", actor="person")
    assert fleet.health_check is None
