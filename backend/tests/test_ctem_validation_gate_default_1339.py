"""The OpenAEV validation gate waits out of the box (Sam, 23 Nov 2026, #1339).

Pinned here without a database:
- on every CTEM loop fleet with the OpenAEV validation step, the validation gate
  waits at the seeded level (two gates wait: validation and Mobilize), none at
  Full autonomous, every gate at Full human gates; a ``run`` pin lifts it and
  survives a reseed; an existing fleet with no pin gets the new default;
- every call that creates or starts a validation pauses at the seeded level,
  and the reads do not;
- one decision per validation: an approved call covers the later create and
  launch calls of the same validation (the objects it named or created), and a
  new target, a new schedule or another scenario asks again.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_fleet_docs import (
    BUILTIN_FLEET_STRUCTURED,
    CTEM_MOBILIZE_GATE,
    CTEM_VALIDATION_ACTION,
    CTEM_VALIDATION_GATE,
)
from app.agents.fleet_seed import SEEDED_FLEET_AUTONOMY_LEVEL, _seed_approval_gates
from app.services.fleet_autonomy import effective_gates, gate_catalog, gated_tool_rules, make_fleet_autonomy_gate
from app.services.fleet_docs import clean_structured
from app.tools.base import HumanInputRequestedError

_VALIDATION_FLEETS = ("ctem-loop", "ctem-vuln-prioritization", "ctem-ai-security")
_SCENARIO = "11111111-1111-4111-8111-111111111111"
_OTHER_SCENARIO = "22222222-2222-4222-8222-222222222222"
_TEST = "33333333-3333-4333-8333-333333333333"
_ASSET = "44444444-4444-4444-8444-444444444444"

#: Every call that creates or starts a validation, one per gated tool and action.
_GATED: list[tuple[str, dict[str, Any]]] = [
    ("create_openaev_scenario", {"name": "Reproduce the threat"}),
    ("import_openaev_scenario", {"file_id": "f"}),
    ("manage_openaev_scenario", {"action": "launch", "scenario_id": _SCENARIO}),
    (
        "manage_openaev_scenario",
        {"action": "schedule_recurrence", "scenario_id": _SCENARIO, "recurrence_cron": "0 0 2 * * *"},
    ),
    ("manage_openaev_scenario", {"action": "duplicate", "scenario_id": _SCENARIO}),
    ("manage_openaev_simulation", {"action": "create", "name": "Run"}),
    ("manage_openaev_simulation", {"action": "duplicate", "exercise_id": _SCENARIO}),
    ("manage_openaev_simulation", {"action": "status", "exercise_id": _SCENARIO, "status": "RUNNING"}),
    ("add_openaev_inject_to_scenario", {"scenario_id": _SCENARIO, "title": "t", "injector_contract_id": "c"}),
    ("manage_openaev_inject", {"action": "create", "scope": "scenario", "parent_id": _SCENARIO}),
    ("manage_openaev_inject", {"action": "duplicate", "scope": "simulation", "parent_id": _SCENARIO}),
    ("manage_openaev_inject", {"action": "trigger", "scope": "simulation", "parent_id": _SCENARIO}),
    ("create_openaev_atomic_testing", {"title": "t", "injector_contract_id": "c", "asset_ids": [_ASSET]}),
    ("manage_openaev_atomic_testing", {"action": "create", "title": "t"}),
    ("manage_openaev_atomic_testing", {"action": "duplicate", "inject_id": _TEST}),
    ("manage_openaev_atomic_testing", {"action": "launch", "inject_id": _TEST}),
    ("manage_openaev_atomic_testing", {"action": "relaunch", "inject_id": _TEST}),
    ("manage_openaev_atomic_testing", {"action": "recurrence", "inject_id": _TEST, "recurrence": "P1D"}),
    ("manage_opencti_security_coverage", {"action": "create", "covered_id": "x"}),
]

#: Reads stay free at every level.
_FREE: list[tuple[str, dict[str, Any]]] = [
    ("search_openaev_scenarios", {}),
    ("get_openaev_scenario", {"scenario_id": _SCENARIO}),
    ("manage_openaev_scenario", {"action": "healthcheck", "scenario_id": _SCENARIO}),
    ("manage_openaev_simulation", {"action": "list"}),
    ("manage_openaev_simulation", {"action": "get", "exercise_id": _SCENARIO}),
    ("manage_openaev_simulation", {"action": "results", "exercise_id": _SCENARIO}),
    ("manage_openaev_simulation", {"action": "status", "exercise_id": _SCENARIO, "status": "PAUSED"}),
    ("manage_openaev_inject", {"action": "list", "scope": "scenario", "parent_id": _SCENARIO}),
    ("manage_openaev_inject", {"action": "status", "scope": "global", "inject_id": _TEST}),
    ("manage_openaev_atomic_testing", {"action": "list"}),
    ("manage_openaev_atomic_testing", {"action": "get", "inject_id": _TEST}),
    ("manage_opencti_security_coverage", {"action": "list"}),
]


def _gates(slug: str) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = BUILTIN_FLEET_STRUCTURED[slug]["gates"]
    return gates


def _call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def _gate(
    level: str, slug: str = "ctem-loop", *, pins: dict[str, str] | None = None, approved: set[str] | None = None
) -> Any:
    gates = effective_gates(_gates(slug), None, pins=pins) if pins else _gates(slug)
    return make_fleet_autonomy_gate(None, gated_tool_rules(level, gates), approved_call_ids=approved)


def _pauses(gate: Any, call: Any) -> bool:
    try:
        gate(call)
    except HumanInputRequestedError:
        return True
    return False


def _done(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, is_error=False)


@pytest.mark.parametrize("slug", _VALIDATION_FLEETS)
def test_two_gates_wait_at_the_seeded_level_none_autonomous_all_at_full_gates(slug: str) -> None:
    def waiting(level: str) -> set[str]:
        return {e["key"] for e in gate_catalog(level, _gates(slug)) if e["gated"]}

    assert waiting(SEEDED_FLEET_AUTONOMY_LEVEL) == {CTEM_VALIDATION_GATE, CTEM_MOBILIZE_GATE}
    assert waiting("autonomous") == set()
    assert waiting("full_gates") == {g["key"] for g in _gates(slug)}


def test_the_third_party_fleet_has_no_validation_tool_and_its_gate_does_not_wait() -> None:
    gate = next(g for g in _gates("ctem-third-party-risk") if g["key"] == CTEM_VALIDATION_GATE)
    assert not gate.get("tools") and "grant" not in gate
    waiting = {
        e["key"] for e in gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, _gates("ctem-third-party-risk")) if e["gated"]
    }
    assert waiting == {CTEM_MOBILIZE_GATE}


@pytest.mark.parametrize("slug", _VALIDATION_FLEETS)
def test_the_validation_gate_says_what_it_gates_in_plain_words(slug: str) -> None:
    gate = next(g for g in _gates(slug) if g["key"] == CTEM_VALIDATION_GATE)
    assert gate["action"] == CTEM_VALIDATION_ACTION
    assert gate["impact"] == "destructive"


@pytest.mark.parametrize(("name", "arguments"), _GATED)
def test_every_creating_or_starting_call_pauses_at_the_seeded_level(name: str, arguments: dict[str, Any]) -> None:
    assert _pauses(_gate(SEEDED_FLEET_AUTONOMY_LEVEL), _call(name, arguments))
    assert not _pauses(_gate("autonomous"), _call(name, arguments))


@pytest.mark.parametrize(("name", "arguments"), _FREE)
def test_the_reads_never_pause(name: str, arguments: dict[str, Any]) -> None:
    for level in (SEEDED_FLEET_AUTONOMY_LEVEL, "autonomous"):
        assert not _pauses(_gate(level), _call(name, arguments))


@pytest.mark.parametrize(("name", "arguments"), _GATED)
def test_a_run_pin_lifts_the_validation_gate(name: str, arguments: dict[str, Any]) -> None:
    assert not _pauses(_gate(SEEDED_FLEET_AUTONOMY_LEVEL, pins={CTEM_VALIDATION_GATE: "run"}), _call(name, arguments))


def test_the_catalog_with_its_grant_passes_the_stored_validation() -> None:
    cleaned = clean_structured({"gates": _gates("ctem-loop")}) or {}
    validation = next(g for g in cleaned["gates"] if g["key"] == CTEM_VALIDATION_GATE)
    assert validation["grant"]["objects"] and validation["grant"]["fresh"]


def test_a_run_pin_survives_a_reseed_and_a_fleet_without_pin_gets_the_new_default() -> None:
    pinned: Any = SimpleNamespace(approval_gates=None, approval_pins={CTEM_VALIDATION_GATE: "run"})
    plain: Any = SimpleNamespace(
        approval_gates=[{**g, "impact": "high"} for g in _gates("ctem-loop")], approval_pins=None
    )
    for fleet in (pinned, plain):
        _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-loop"])
        _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-loop"])
    assert pinned.approval_pins == {CTEM_VALIDATION_GATE: "run"}

    def waits(fleet: Any) -> bool:
        gates = effective_gates(fleet.approval_gates, None, pins=fleet.approval_pins)
        catalog = {e["key"]: e["gated"] for e in gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, gates)}
        return bool(catalog[CTEM_VALIDATION_GATE])

    assert waits(plain) is True
    assert waits(pinned) is False


def test_an_approved_creation_covers_the_launch_of_that_validation() -> None:
    gate = _gate(SEEDED_FLEET_AUTONOMY_LEVEL, approved={"create"})
    create = _call(
        "create_openaev_atomic_testing", {"title": "t", "injector_contract_id": "c", "asset_ids": [_ASSET]}, "create"
    )
    gate(create)
    gate.observe(create, _done(f"Atomic test created: [t] (ID: {_TEST})"))
    assert not _pauses(gate, _call("manage_openaev_atomic_testing", {"action": "launch", "inject_id": _TEST}, "launch"))


def test_an_approved_scenario_covers_its_injects_and_its_launch_but_not_another_scenario() -> None:
    gate = _gate(SEEDED_FLEET_AUTONOMY_LEVEL, approved={"create"})
    create = _call("create_openaev_scenario", {"name": "Reproduce"}, "create")
    gate(create)
    gate.observe(create, _done(f"Scenario created: [Reproduce] (ID: {_SCENARIO})"))
    inject = _call(
        "add_openaev_inject_to_scenario", {"scenario_id": _SCENARIO, "title": "t", "injector_contract_id": "c"}, "i"
    )
    assert not _pauses(gate, inject)
    gate.observe(inject, _done("Inject added"))
    assert not _pauses(gate, _call("manage_openaev_scenario", {"action": "launch", "scenario_id": _SCENARIO}, "l"))
    assert _pauses(gate, _call("manage_openaev_scenario", {"action": "launch", "scenario_id": _OTHER_SCENARIO}, "o"))


def test_a_new_target_or_a_new_schedule_asks_again() -> None:
    gate = _gate(SEEDED_FLEET_AUTONOMY_LEVEL, approved={"create"})
    create = _call("create_openaev_scenario", {"name": "Reproduce"}, "create")
    gate(create)
    gate.observe(create, _done(f"Scenario created (ID: {_SCENARIO})"))
    recur = {"action": "schedule_recurrence", "scenario_id": _SCENARIO, "recurrence_cron": "0 0 2 * * *"}
    assert _pauses(gate, _call("manage_openaev_scenario", recur, "r"))
    target = {"action": "create", "scope": "scenario", "parent_id": _SCENARIO, "asset_ids": [_ASSET]}
    assert _pauses(gate, _call("manage_openaev_inject", target, "t"))


def test_a_failed_approved_call_grants_nothing() -> None:
    gate = _gate(SEEDED_FLEET_AUTONOMY_LEVEL, approved={"create"})
    create = _call("create_openaev_scenario", {"name": "Reproduce"}, "create")
    gate(create)
    gate.observe(create, SimpleNamespace(content=f"error on {_SCENARIO}", is_error=True))
    assert _pauses(gate, _call("manage_openaev_scenario", {"action": "launch", "scenario_id": _SCENARIO}, "l"))


def test_an_unapproved_call_grants_nothing() -> None:
    gate = _gate("autonomous")
    gate.observe(_call("create_openaev_scenario", {"name": "x"}), _done(f"(ID: {_SCENARIO})"))
    strict = _gate(SEEDED_FLEET_AUTONOMY_LEVEL)
    strict.observe(_call("create_openaev_scenario", {"name": "x"}), _done(f"(ID: {_SCENARIO})"))
    assert _pauses(strict, _call("manage_openaev_scenario", {"action": "launch", "scenario_id": _SCENARIO}, "l"))
