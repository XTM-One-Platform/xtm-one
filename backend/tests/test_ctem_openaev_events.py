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
from app.models.assignment import marked_exact
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
    found = listeners.match(
        "openaev", "scenario_score_degraded", _delivery("scenario_score_degraded", "SCENARIO", "s1")
    )
    assert found is not None
    row, signal = found
    assert row.trigger_path == "mitigation_regression" and row.lens == "inside"
    assert signal["external_id"] == "s1"
    assert "mitigation_regression" in listeners.paths_with_listener()


def test_a_failed_reachability_assertion_is_a_path_5_outside_exposure() -> None:
    # CTEM-26 (#1164): the receiver stamps the scenario's assertion flag on the event.
    delivery = _delivery("scenario_score_degraded", "SCENARIO", "s1")
    delivery["_openaev_event"]["reachability_assertion"] = True
    found = listeners.match("openaev", "scenario_score_degraded", delivery)
    assert found is not None
    row, signal = found
    assert (row.trigger_path, row.lens, row.subject_kind) == ("new_exposure", "outside", "asset")
    assert signal["external_id"] == "s1"
    assert signal["evidence"]["reachability_assertion"] is True
    assert "new_exposure" in listeners.paths_with_listener()


@pytest.mark.parametrize("flag", [False, None, "true"])
def test_a_scenario_that_is_not_an_assertion_stays_path_8(flag: Any) -> None:
    delivery = _delivery("scenario_score_degraded", "SCENARIO", "s1")
    if flag is not None:
        delivery["_openaev_event"]["reachability_assertion"] = flag
    found = listeners.match("openaev", "scenario_score_degraded", delivery)
    assert found is not None
    row, signal = found
    assert row.trigger_path == "mitigation_regression"
    assert "reachability_assertion" not in signal["evidence"]


def _scenario_api(monkeypatch: pytest.MonkeyPatch, body: Any = None, err: str | None = None) -> list[str]:
    calls: list[str] = []

    async def read(integration_id: str, scenario_id: str) -> bool | None:
        calls.append(scenario_id)
        if err:
            raise RuntimeError(err)
        value = body.get(aev.ASSERTION_FIELD) if isinstance(body, dict) else None
        return value if isinstance(value, bool) else None

    monkeypatch.setattr(aev, "_read_assertion_flag", read)
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    return calls


@pytest.mark.asyncio
async def test_the_receiver_stamps_the_assertion_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scenario_api(monkeypatch, {"scenario_id": "s1", aev.ASSERTION_FIELD: True})
    delivery = _delivery("scenario_score_degraded", "SCENARIO", "s1")
    flag = await aev.stamp_reachability_assertion(
        "scenario_score_degraded", delivery, integration_id=str(uuid.uuid4()), verified=True
    )
    assert flag is True and calls == ["s1"]
    assert delivery["_openaev_event"]["reachability_assertion"] is True


@pytest.mark.asyncio
async def test_the_receiver_reads_nothing_for_other_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scenario_api(monkeypatch, {aev.ASSERTION_FIELD: True})
    unverified = _delivery("scenario_score_degraded", "SCENARIO", "s1")
    assert (
        await aev.stamp_reachability_assertion(
            "scenario_score_degraded", unverified, integration_id="i", verified=False
        )
        is None
    )
    other = _delivery("asset_stale", "ASSET", "a1")
    assert await aev.stamp_reachability_assertion("asset_stale", other, integration_id="i", verified=True) is None
    assert calls == []
    assert "reachability_assertion" not in unverified["_openaev_event"]


@pytest.mark.asyncio
async def test_an_unreadable_scenario_keeps_path_8(monkeypatch: pytest.MonkeyPatch) -> None:
    _scenario_api(monkeypatch, err="boom")
    delivery = _delivery("scenario_score_degraded", "SCENARIO", "s1")
    assert (
        await aev.stamp_reachability_assertion(
            "scenario_score_degraded", delivery, integration_id=str(uuid.uuid4()), verified=True
        )
        is None
    )
    assert "reachability_assertion" not in delivery["_openaev_event"]
    found = listeners.match("openaev", "scenario_score_degraded", delivery)
    assert found is not None and found[0].trigger_path == "mitigation_regression"


def test_a_critical_asset_reached_is_a_path_4_signal() -> None:
    # CTEM-13: OpenAEV names the simulation only; the subject is the run, linked back to OpenAEV.
    found = listeners.match(
        "openaev",
        "critical_asset_reached",
        _delivery("critical_asset_reached", "SIMULATION", "sim-1", "[simulation] Web"),
    )
    assert found is not None
    row, signal = found
    assert row.trigger_path == "proven_exploitable" and row.lens == "inside"
    assert signal["external_id"] == "sim-1"
    assert signal["evidence"]["event"] == "critical_asset_reached"
    assert signal["evidence"]["platform_url"] == "https://aev.example"
    assert "proven_exploitable" in listeners.paths_with_listener()
    # The same event on another resource records nothing.
    assert (
        listeners.match("openaev", "critical_asset_reached", _delivery("critical_asset_reached", "SCENARIO", "x"))
        is None
    )
    # A case that waits on the simulation is moved by it.
    assert "critical_asset_reached" in aev.CASE_EVENT_KEYS


def test_the_stale_asset_trigger_filters_on_the_stale_status() -> None:
    # CTEM-27: OpenAEV's freshness job raises an asset update on the STALE transition;
    # the trigger keeps only updates of an asset whose status reads STALE.
    spec = {"fleet": "F", "purpose": "P", "events": ["asset_stale"]}
    body = prov.trigger_body(spec, "asset_stale", "n1")
    assert body["notification_trigger_resource_type"] == "ASSET"
    assert body["notification_trigger_event_types"] == ["UPDATE"]
    assert body["notification_trigger_filters"] == {
        "mode": "and",
        "filters": [{"key": "asset_status", "operator": "eq", "mode": "or", "values": ["STALE"]}],
    }
    assert prov.event_from_notification_name(body["notification_trigger_name"]) == "asset_stale"
    assert "asset_stale" in CTEM_OPENAEV_LOOP_EVENTS


def test_a_stale_asset_is_a_path_6_signal() -> None:
    found = listeners.match("openaev", "asset_stale", _delivery("asset_stale", "ASSET", "a1", "[asset] web-01 updated"))
    assert found is not None
    row, signal = found
    assert (row.trigger_path, row.lens, row.subject_kind) == ("stale_asset", "outside", "asset")
    assert signal["external_id"] == "a1"
    assert signal["evidence"]["event"] == "asset_stale"
    assert signal["evidence"]["platform_url"] == "https://aev.example"
    assert "stale_asset" in listeners.paths_with_listener()


def test_a_plain_asset_update_records_no_stale_signal() -> None:
    # Only the filtered trigger names the event asset_stale; a plain asset update is no signal.
    assert listeners.match("openaev", "asset_updated", _delivery("asset_updated", "ASSET", "a1")) is None
    assert listeners.match("openaev", "asset_stale", _delivery("asset_stale", "SCENARIO", "s1")) is None


def test_other_openaev_events_record_no_signal() -> None:
    assert (
        listeners.match("openaev", "simulation_finished", _delivery("simulation_finished", "SIMULATION", "x")) is None
    )
    assert (
        listeners.match("openaev", "scenario_score_degraded", _delivery("scenario_score_degraded", "FINDING", "x"))
        is None
    )
    assert listeners.match("openaev", "scenario_score_degraded", {}) is None


def _case(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "vertical": "cyber",
        "state": "remediating",
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


def test_one_live_frame_per_case_moved_ids_and_states_only() -> None:
    """CTEM-15-UI (#956): the case page re-reads at once, the frame carries no title or evidence."""
    from datetime import UTC, datetime

    at = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    pending = [
        (
            aev.CASE_VALIDATION_FINISHED,
            {"case_id": "c1", "state": "remediating", "openaev": {"resource_id": "x"}},
            "e1",
        ),
        (aev.CASE_VALIDATION_FINISHED, {"case_id": "c1", "state": "remediating"}, "e2"),
        (aev.CASE_VALIDATION_FINISHED, {"case_id": "c2", "state": "resolved"}, "e3"),
        (aev.CASE_VALIDATION_FINISHED, {"state": "resolved"}, "e4"),
    ]
    frames = aev.case_frames(pending, at)
    assert frames == [
        {
            "type": "ctem_case_update",
            "case_id": "c1",
            "state": "remediating",
            "event_kind": aev.HISTORY_KIND,
            "at": at.isoformat(),
        },
        {
            "type": "ctem_case_update",
            "case_id": "c2",
            "state": "resolved",
            "event_kind": aev.HISTORY_KIND,
            "at": at.isoformat(),
        },
    ]
    assert aev.case_frames([], at) == []


async def test_an_unverified_delivery_moves_nothing() -> None:
    assert (
        await aev.note_event("simulation_finished", _delivery("simulation_finished", "SIMULATION", "s"), verified=False)
        == 0
    )


def test_the_case_event_is_registered_and_heard_by_exactly_one_fleet() -> None:
    assert aev.CASE_VALIDATION_FINISHED in platform_events.PLATFORM_EVENTS
    # A case event reaches only the readers of the case (#933).
    assert aev.CASE_VALIDATION_FINISHED not in platform_events.PLATFORM_WIDE_EVENTS
    assert aev.CASE_VALIDATION_FINISHED in platform_events.VIEWER_CHECKED_EVENTS
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        assert aev.CASE_VALIDATION_FINISHED in assignment["trigger_config"]["event"]
        assert "record_ctem_case_validation" in assignment["prompt"]


def test_the_listen_only_assignment_provisions_and_never_runs() -> None:
    assert CTEM_F2_OPENAEV_ASSIGNMENT in BUILTIN_ASSIGNMENTS
    config = CTEM_F2_OPENAEV_ASSIGNMENT["trigger_config"]
    assert set(prov.read_provision_spec(config)["events"]) == set(CTEM_OPENAEV_LOOP_EVENTS)
    assert set(CTEM_OPENAEV_LOOP_EVENTS) <= set(prov.EVENTS)
    # Seeded from scratch: exact matching, and its selection names every event it listens to.
    assert marked_exact(config)
    assert config["events"] == list(CTEM_OPENAEV_LOOP_EVENTS) and "event" not in config
    assert set(aev.CASE_EVENT_KEYS) <= set(CTEM_OPENAEV_LOOP_EVENTS)
    for event in CTEM_OPENAEV_LOOP_EVENTS:
        assert payload_filter_reason(_delivery(event, "SIMULATION", "x"), config["filter"]) is not None
    assert CTEM_F2_OPENAEV_ASSIGNMENT["enabled"] is False and CTEM_F2_OPENAEV_ASSIGNMENT["admin_opt_in"] is True
