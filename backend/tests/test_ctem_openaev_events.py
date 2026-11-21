"""OpenAEV events into the CTEM loop (CTEM-15, #918).

The new OpenAEV events are provisioned like the first three; a scenario score
degradation is a path 8 signal; a finished simulation or a changed finding a
case waits on raises one ``ctem_case.validation_finished`` per case; the loop
fleet listens to it; the listen-only assignment never starts a run.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_agents import (
    BUILTIN_ASSIGNMENTS,
    CTEM_F1_CASE_ASSIGNMENT,
    CTEM_F2_CASE_ASSIGNMENT,
    CTEM_F2_OPENAEV_ASSIGNMENT,
    CTEM_OPENAEV_LOOP_EVENTS,
)
from app.services import ctem_openaev_events as aev
from app.services import ctem_signal_listeners as listeners
from app.services import openaev_trigger_provisioning as prov
from app.services import platform_events
from app.services.integration_event_filters import payload_filter_reason

NEW_EVENTS = {
    "finding_updated": ("FINDING", "UPDATE"),
    "finding_deleted": ("FINDING", "DELETE"),
    "asset_created": ("ASSET", "CREATE"),
    "asset_updated": ("ASSET", "UPDATE"),
    "vulnerability_created": ("VULNERABILITY", "CREATE"),
    "scenario_score_degraded": ("SCENARIO", "SCORE_DEGRADATION"),
}


def _delivery(event: str, resource_type: str, resource_id: str, message: str = "m") -> dict[str, Any]:
    return {
        "_openaev_event": {
            "event_type": event,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "message": message,
            "platform_url": "https://aev.example",
        }
    }


@pytest.mark.parametrize(("event", "expected"), sorted(NEW_EVENTS.items()))
def test_the_new_events_are_provisioned(event: str, expected: tuple[str, str]) -> None:
    spec = {"fleet": "F", "purpose": "P", "events": [event]}
    body = prov.trigger_body(spec, event, "n1")
    assert (body["notification_trigger_resource_type"], body["notification_trigger_event_types"]) == (
        expected[0],
        [expected[1]],
    )
    assert "notification_trigger_filters" not in body
    assert prov.event_from_notification_name(body["notification_trigger_name"]) == event


def test_the_score_degradation_is_a_path_8_signal() -> None:
    found = listeners.match("openaev", "scenario_score_degraded", _delivery("scenario_score_degraded", "SCENARIO", "s1"))
    assert found is not None
    row, signal = found
    assert row.trigger_path == "mitigation_regression" and row.lens == "inside"
    assert signal["external_id"] == "s1"
    assert "mitigation_regression" in listeners.paths_with_listener()


def test_other_openaev_events_record_no_signal() -> None:
    assert listeners.match("openaev", "simulation_finished", _delivery("simulation_finished", "SIMULATION", "x")) is None
    assert listeners.match("openaev", "scenario_score_degraded", _delivery("scenario_score_degraded", "FINDING", "x")) is None
    assert listeners.match("openaev", "scenario_score_degraded", {}) is None


def _case(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "vertical": "cyber",
        "state": "in_progress",
        "remediation_kind": "patch",
        "trigger_path": "new_vulnerability",
        "severity": "P1",
        "sla_due_at": None,
        "owner_user_id": None,
        "risk_scenario_ids": [],
        "validations": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_case_waits_on_its_validation_reference_or_its_member() -> None:
    case = _case(validations=[{"provider": "openaev", "ref": "simulation sim-1", "result": "pending"}])
    assert aev.references(case, [], "sim-1")
    assert not aev.references(case, [], "sim-2")
    other = _case(validations=[{"provider": "opencti", "ref": "sim-1"}])
    assert not aev.references(other, [], "sim-1")
    member = SimpleNamespace(source="openaev", external_id="f-9")
    assert aev.references(_case(), [member], "f-9")
    assert not aev.references(_case(), [member], "")


def test_one_event_per_case_and_occurrence() -> None:
    case = _case()
    facts = {"resource_id": "sim-1", "message": "a"}
    first = aev.event_id(case, "simulation_finished", facts)
    assert first == aev.event_id(case, "simulation_finished", dict(facts))
    assert first != aev.event_id(case, "simulation_finished", {**facts, "message": "b"})
    assert first != aev.event_id(_case(), "simulation_finished", facts)


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


async def test_route_raises_one_event_per_waiting_case(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = _case(validations=[{"provider": "openaev", "ref": "sim-1"}])

    async def _matching(_db: Any, resource_id: str) -> list[Any]:
        return [waiting] if resource_id == "sim-1" else []

    monkeypatch.setattr(aev, "matching_cases", _matching)
    db = _Db()
    out = await aev.route(db, "simulation_finished", _delivery("simulation_finished", "SIMULATION", "sim-1"))
    assert len(out) == 1
    name, data, _eid = out[0]
    assert name == aev.CASE_VALIDATION_FINISHED
    assert data["case_id"] == str(waiting.id) and data["trigger_path"] == "new_vulnerability"
    assert data["openaev"] == {
        "event": "simulation_finished",
        "resource_type": "SIMULATION",
        "resource_id": "sim-1",
        "platform_url": "https://aev.example",
    }
    assert [r.kind for r in db.added] == [aev.HISTORY_KIND]
    assert len(aev.HISTORY_KIND) <= 20
    assert await aev.route(db, "simulation_finished", _delivery("simulation_finished", "SIMULATION", "nope")) == []
    assert await aev.route(db, "asset_created", _delivery("asset_created", "ASSET", "sim-1")) == []


async def test_an_unverified_delivery_moves_nothing() -> None:
    assert await aev.note_event("simulation_finished", _delivery("simulation_finished", "SIMULATION", "s"), verified=False) == 0


def test_the_case_event_is_registered_and_heard_by_exactly_one_fleet() -> None:
    assert aev.CASE_VALIDATION_FINISHED in platform_events.PLATFORM_EVENTS
    assert aev.CASE_VALIDATION_FINISHED in platform_events.PLATFORM_WIDE_EVENTS
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        assert aev.CASE_VALIDATION_FINISHED in assignment["trigger_config"]["event"]
        assert "record_ctem_case_validation" in assignment["prompt"]


def test_the_listen_only_assignment_provisions_and_never_runs() -> None:
    assert CTEM_F2_OPENAEV_ASSIGNMENT in BUILTIN_ASSIGNMENTS
    config = CTEM_F2_OPENAEV_ASSIGNMENT["trigger_config"]
    assert prov.read_provision_spec(config)["events"] == list(CTEM_OPENAEV_LOOP_EVENTS)
    assert set(CTEM_OPENAEV_LOOP_EVENTS) <= set(prov.EVENTS)
    assert set(aev.CASE_EVENT_KEYS) <= set(CTEM_OPENAEV_LOOP_EVENTS)
    for event in CTEM_OPENAEV_LOOP_EVENTS:
        assert payload_filter_reason(_delivery(event, "SIMULATION", "x"), config["filter"]) is not None
    assert CTEM_F2_OPENAEV_ASSIGNMENT["enabled"] is False and CTEM_F2_OPENAEV_ASSIGNMENT["admin_opt_in"] is True
