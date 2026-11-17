"""The ``fleet`` subscription scope of a ``platform_event`` assignment trigger.

A fleet's orchestrator listens to the events of its desks' runs: ``own`` never
hears them (the orchestrator takes no part in a desk's run) and ``all`` hears
every agent of the platform. ``fleet`` delivers the events of the home members
of the assignment agent's fleet, membership read at dispatch.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import platform_events as pe

ORCHESTRATOR = uuid.uuid4()
DESK = uuid.uuid4()
OUTSIDER = uuid.uuid4()
FLEET = uuid.uuid4()
OTHER_FLEET = uuid.uuid4()
EVENT = "run.completed_with_undisposed_frictions"


def _sub(scope: str, **extra) -> pe.Subscription:
    base = {
        "assignment_id": uuid.uuid4(),
        "agent_id": ORCHESTRATOR,
        "events": (EVENT,),
        "filter": {},
        "scope_all": scope == "all",
    }
    if scope == "fleet":
        base["fleet_agent_ids"] = frozenset({str(ORCHESTRATOR), str(DESK)})
    base.update(extra)
    return pe.Subscription(**base)


def _payload(agent: uuid.UUID) -> dict:
    data = pe.undisposed_frictions_data(run_id=uuid.uuid4(), agent_id=agent, friction_ids=["f1"])
    return {**data, "event": EVENT, "event_id": f"e-{agent}"}


# -- Validation ------------------------------------------------------------------


def test_fleet_is_a_valid_assignment_scope():
    assert pe.trigger_config_error("platform_event", {"event": EVENT, "scope": "fleet"}) is None
    error = pe.trigger_config_error("platform_event", {"event": EVENT, "scope": "team"})
    assert error is not None and "'fleet'" in error


@pytest.mark.asyncio
async def test_fleet_scope_is_refused_for_an_agent_in_no_fleet(monkeypatch):
    from app.services import coordination_scope

    async def home(agent_id):
        return (FLEET, "Fleet", (agent_id,), None) if agent_id == ORCHESTRATOR else None

    monkeypatch.setattr(coordination_scope, "read_home_fleet", home)
    config = {"event": EVENT, "scope": "fleet"}
    assert await pe.fleet_scope_error("platform_event", config, ORCHESTRATOR) is None
    error = await pe.fleet_scope_error("platform_event", config, OUTSIDER)
    assert error is not None and str(OUTSIDER) in error and "home fleet" in error
    # Any other scope or trigger type is not judged.
    assert await pe.fleet_scope_error("platform_event", {"event": EVENT, "scope": "own"}, OUTSIDER) is None
    assert await pe.fleet_scope_error("schedule", config, OUTSIDER) is None


# -- in_scope --------------------------------------------------------------------


def test_the_three_scopes_on_a_desk_an_outsider_and_the_orchestrator():
    own, fleet, every = _sub("own"), _sub("fleet"), _sub("all")
    desk, outsider, mine = _payload(DESK), _payload(OUTSIDER), _payload(ORCHESTRATOR)
    assert [pe.in_scope(s, desk) for s in (own, fleet, every)] == [False, True, True]
    assert [pe.in_scope(s, outsider) for s in (own, fleet, every)] == [False, False, True]
    assert [pe.in_scope(s, mine) for s in (own, fleet, every)] == [True, True, True]


def test_fleet_scope_hears_a_member_named_only_as_participant():
    payload = {"event": EVENT, "agent_id": str(OUTSIDER), "participant_agent_ids": [str(OUTSIDER), str(DESK)]}
    assert pe.in_scope(_sub("fleet"), payload)


def test_a_fleet_subscriber_left_with_no_fleet_falls_back_to_own():
    lone = _sub("fleet", fleet_agent_ids=frozenset())
    assert not pe.in_scope(lone, _payload(DESK))
    assert pe.in_scope(lone, _payload(ORCHESTRATOR))


def test_fleet_scope_keeps_personal_objects_to_their_owner():
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    payload = {**_payload(DESK), "personal_owner_id": str(owner)}
    assert pe.in_scope(_sub("fleet", user_id=owner), payload)
    assert not pe.in_scope(_sub("fleet", user_id=stranger), payload)


# -- Membership read at dispatch -------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _MembershipDb:
    """Answers the two reads of ``_fleet_members`` from a list of home rows.

    *asked* is the subscribers' agents the first read selects; the second
    read selects the members of the fleets that read found.
    """

    def __init__(self, homes: list[tuple[uuid.UUID, uuid.UUID]], asked: set[uuid.UUID]):
        self.homes = homes
        self.asked = asked
        self.reads = 0

    async def execute(self, statement):
        self.reads += 1
        if self.reads == 1:
            return _Result([(a, f) for a, f in self.homes if a in self.asked])
        fleets = {f for a, f in self.homes if a in self.asked}
        return _Result([(f, a) for a, f in self.homes if f in fleets])


@pytest.mark.asyncio
async def test_membership_is_read_at_dispatch_so_a_change_needs_no_trigger_edit():
    db = _MembershipDb([(ORCHESTRATOR, FLEET), (DESK, FLEET), (OUTSIDER, OTHER_FLEET)], {ORCHESTRATOR})
    members = await pe._fleet_members(db, {ORCHESTRATOR})
    assert members == {ORCHESTRATOR: frozenset({str(ORCHESTRATOR), str(DESK)})}
    assert db.reads == 2

    # The outsider joins the fleet, the desk leaves it: the next dispatch follows.
    db = _MembershipDb([(ORCHESTRATOR, FLEET), (OUTSIDER, FLEET)], {ORCHESTRATOR})
    members = await pe._fleet_members(db, {ORCHESTRATOR})
    sub = _sub("fleet", fleet_agent_ids=members[ORCHESTRATOR])
    assert pe.in_scope(sub, _payload(OUTSIDER))
    assert not pe.in_scope(sub, _payload(DESK))


@pytest.mark.asyncio
async def test_no_fleet_subscriber_reads_nothing():
    db = _MembershipDb([], {OUTSIDER})
    assert await pe._fleet_members(db, set()) == {}
    assert db.reads == 0
    assert await pe._fleet_members(db, {OUTSIDER}) == {}
    assert db.reads == 1


@pytest.mark.asyncio
async def test_dispatch_delivers_by_scope(monkeypatch):
    import app.core.database as database

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", _Session)
    own, fleet, every = _sub("own"), _sub("fleet"), _sub("all")
    row = SimpleNamespace(ignore_self_triggered_events=False)
    delivered: list = []

    async def subscriptions(db, name):
        return [(own, row, None), (fleet, row, None), (every, row, None)]

    async def deliver(name, payload, sub, assignment, agent):
        delivered.append(sub.assignment_id)
        return "ok"

    monkeypatch.setattr(pe, "_subscriptions", subscriptions)
    monkeypatch.setattr(pe, "_deliver", deliver)

    await pe.dispatch_event(EVENT, _payload(DESK))
    assert delivered == [fleet.assignment_id, every.assignment_id]
    delivered.clear()
    await pe.dispatch_event(EVENT, _payload(OUTSIDER))
    assert delivered == [every.assignment_id]
