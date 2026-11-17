"""Fleets phase 6: run defaults, fleet ids in trace metadata, cost by fleet.

Unit tests of ``app.services.fleet_runs`` and of the two resolution paths that
read it (assignment iteration cap, the fleet API's authority rule), with a
fake session: no database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import fleet_runs
from app.services.fleet_runs import (
    CALLER_FLEET_ID_KEY,
    FLEET_ID_KEY,
    NO_FLEET,
    TARGET_FLEET_ID_KEY,
    FleetRunDefaults,
    chat_caller_agent_id,
    fleet_trace_metadata,
    member_budget_overrides,
    member_max_iterations,
)

FLEET_A = uuid.uuid4()
FLEET_B = uuid.uuid4()
AGENT = uuid.uuid4()
CALLER = uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Answers every ``execute`` with the next queued row list."""

    def __init__(self, *answers, fail: bool = False):
        self._answers = list(answers)
        self.calls = 0
        self._fail = fail

    async def execute(self, _query):
        self.calls += 1
        if self._fail:
            raise RuntimeError("database unavailable")
        return _Result(self._answers.pop(0) if self._answers else [])


# ── Resolution: agent, then home fleet, then platform ───────────────────────


def test_agent_budget_wins_per_dimension_over_its_fleet():
    fleet = FleetRunDefaults(fleet_id=FLEET_A, budget_config={"max_cost_usd": 5.0, "max_tool_calls": 40})
    merged = member_budget_overrides({"max_cost_usd": 1.0}, fleet)
    assert merged.max_cost_usd == 1.0
    assert merged.max_tool_calls == 40


def test_no_fleet_or_empty_fleet_budget_keeps_the_agent_budget_as_is():
    own = {"max_cost_usd": 2.0}
    assert member_budget_overrides(own, None).to_dict() == member_budget_overrides(own, NO_FLEET).to_dict()
    assert member_budget_overrides(None, NO_FLEET).is_empty
    assert member_budget_overrides(None, FleetRunDefaults(fleet_id=FLEET_A, budget_config={})).is_empty


def test_iteration_cap_agent_then_fleet_then_platform():
    fleet = FleetRunDefaults(fleet_id=FLEET_A, max_iterations=30)
    assert member_max_iterations(12, fleet) == 12
    assert member_max_iterations(None, fleet) == 30
    assert member_max_iterations(None, NO_FLEET) is None
    assert member_max_iterations(None, None) is None


async def test_home_fleet_defaults_reads_the_member_row():
    db = _FakeDB([(AGENT, FLEET_A, {"max_cost_usd": 3.0}, 25, {"mode": "primary"}, None)])
    got = await fleet_runs.home_fleet_defaults(db, str(AGENT))
    assert got == FleetRunDefaults(
        fleet_id=FLEET_A, budget_config={"max_cost_usd": 3.0}, max_iterations=25, organization_scope={"mode": "primary"}
    )


async def test_home_fleet_defaults_carries_the_member_organization_scope():
    """The member row's scope wins over the fleet's (R3 of #9162)."""
    db = _FakeDB([(AGENT, FLEET_A, None, None, {"mode": "monitored"}, {"mode": "primary"})])
    assert (await fleet_runs.home_fleet_defaults(db, str(AGENT))).organization_scope == {"mode": "primary"}
    db = _FakeDB([(AGENT, FLEET_A, None, None, {"mode": "monitored"}, None)])
    assert (await fleet_runs.home_fleet_defaults(db, str(AGENT))).organization_scope == {"mode": "monitored"}


async def test_home_fleet_defaults_playground_agent_and_failed_read_are_no_fleet():
    assert await fleet_runs.home_fleet_defaults(_FakeDB([]), AGENT) is NO_FLEET
    assert await fleet_runs.home_fleet_defaults(_FakeDB(fail=True), AGENT) is NO_FLEET
    db = _FakeDB()
    assert await fleet_runs.home_fleet_defaults(db, None) is NO_FLEET
    assert db.calls == 0


async def test_assignment_iteration_cap_reads_the_fleet_only_when_nothing_pins_one(monkeypatch):
    from app.services import assignment_engine

    reads: list[object] = []

    async def _defaults(_db, agent_id):
        reads.append(agent_id)
        return FleetRunDefaults(fleet_id=FLEET_A, max_iterations=33)

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _defaults)
    agent = SimpleNamespace(id=AGENT, max_iterations=None)

    # The platform row read answers 50.
    got = await assignment_engine.resolve_run_max_iterations(
        _FakeDB([50]), SimpleNamespace(max_iterations=None), agent
    )
    assert got == 33
    assert reads == [AGENT]

    reads.clear()
    pinned = await assignment_engine.resolve_run_max_iterations(
        _FakeDB([50]), SimpleNamespace(max_iterations=7), agent
    )
    assert pinned == 7
    assert reads == []

    own = await assignment_engine.resolve_run_max_iterations(
        _FakeDB([50]), SimpleNamespace(max_iterations=None), SimpleNamespace(id=AGENT, max_iterations=9)
    )
    assert own == 9
    assert reads == []


# ── Trace metadata ───────────────────────────────────────────────────────────


def test_trace_metadata_in_fleet_run_carries_fleet_id_only():
    target = FleetRunDefaults(fleet_id=FLEET_A)
    assert fleet_trace_metadata(target) == {FLEET_ID_KEY: str(FLEET_A)}
    assert fleet_trace_metadata(target, FleetRunDefaults(fleet_id=FLEET_A)) == {FLEET_ID_KEY: str(FLEET_A)}


def test_trace_metadata_cross_fleet_run_carries_caller_and_target():
    meta = fleet_trace_metadata(FleetRunDefaults(fleet_id=FLEET_B), FleetRunDefaults(fleet_id=FLEET_A))
    assert meta == {
        FLEET_ID_KEY: str(FLEET_B),
        CALLER_FLEET_ID_KEY: str(FLEET_A),
        TARGET_FLEET_ID_KEY: str(FLEET_B),
    }


def test_trace_metadata_playground_on_either_side_is_not_cross_fleet():
    assert fleet_trace_metadata(NO_FLEET, FleetRunDefaults(fleet_id=FLEET_A)) == {}
    assert fleet_trace_metadata(FleetRunDefaults(fleet_id=FLEET_B), NO_FLEET) == {FLEET_ID_KEY: str(FLEET_B)}
    assert fleet_trace_metadata(None, None) == {}


async def test_run_fleet_trace_metadata_reads_caller_and_target_in_one_query():
    db = _FakeDB([(AGENT, FLEET_B, None, None, None, None), (CALLER, FLEET_A, None, None, None, None)])
    meta = await fleet_runs.run_fleet_trace_metadata(db, AGENT, str(CALLER))
    assert meta[CALLER_FLEET_ID_KEY] == str(FLEET_A)
    assert meta[TARGET_FLEET_ID_KEY] == str(FLEET_B)
    assert db.calls == 1


async def test_run_fleet_trace_metadata_reuses_a_resolved_target():
    db = _FakeDB()
    meta = await fleet_runs.run_fleet_trace_metadata(db, AGENT, None, target=FleetRunDefaults(fleet_id=FLEET_A))
    assert meta == {FLEET_ID_KEY: str(FLEET_A)}
    assert db.calls == 0


async def test_run_fleet_trace_metadata_failed_read_stamps_nothing():
    assert await fleet_runs.run_fleet_trace_metadata(_FakeDB(fail=True), AGENT, CALLER) == {}


def test_chat_caller_is_the_last_ancestor_of_the_chain():
    assert chat_caller_agent_id([str(CALLER), str(AGENT)]) == str(CALLER)
    assert chat_caller_agent_id([str(AGENT)]) is None
    assert chat_caller_agent_id(None) is None


async def test_parent_run_agent_id():
    assert await fleet_runs.parent_run_agent_id(_FakeDB([CALLER]), uuid.uuid4()) == CALLER
    assert await fleet_runs.parent_run_agent_id(_FakeDB(), None) is None
    assert await fleet_runs.parent_run_agent_id(_FakeDB(fail=True), uuid.uuid4()) is None


# ── Cost by fleet ────────────────────────────────────────────────────────────


async def test_fleet_cost_sorts_costliest_first_and_coerces_numbers():
    other = uuid.uuid4()
    db = _FakeDB([(AGENT, "Desk", 3, 0.25, 900), (other, None, 1, 1.5, 100)])
    costs = await fleet_runs.fleet_cost(db, FLEET_A, since=datetime.now(UTC))
    assert [c.agent_id for c in costs] == [other, AGENT]
    assert costs[0].agent_name == ""
    assert costs[1].total_cost == pytest.approx(0.25)
    assert costs[1].trace_count == 3


# ── Authority: a fleet default under the platform ceiling ────────────────────


def test_non_admin_fleet_budget_above_the_ceiling_is_refused(monkeypatch):
    from app.api.v1.helpers import assert_budget_within_ceiling
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_max_cost_usd", 10.0)
    user = SimpleNamespace(is_admin=False)
    with pytest.raises(HTTPException) as exc:
        assert_budget_within_ceiling({"max_cost_usd": 50.0}, user=user, what="the fleet 'Ops'")
    assert exc.value.status_code == 403
    assert "the fleet 'Ops'" in exc.value.detail
    assert assert_budget_within_ceiling({"max_cost_usd": 5.0}, user=user, what="the fleet 'Ops'") == {
        "max_cost_usd": 5.0
    }
    assert assert_budget_within_ceiling(
        {"max_cost_usd": 50.0}, user=SimpleNamespace(is_admin=True), what="the fleet 'Ops'"
    ) == {"max_cost_usd": 50.0}
