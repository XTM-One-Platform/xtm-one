"""The OpenAEV validation gate waits out of the box (Sam, 23 Nov 2026, #1339), as a decision (#1675).

Pinned here without a database:
- on every CTEM loop fleet with the OpenAEV validation step, the validation gate
  waits at the seeded level (two gates wait: validation and Mobilize), none at
  Full autonomous, every gate at Full human gates; a ``run`` pin lifts it and
  survives a reseed; an existing fleet with no pin gets the new default;
- a seeded gate never pauses a tool call (#1675, Sam, 24 Nov 2026): every call
  that creates or starts a validation runs at every level; the wait is one
  decision item the orchestrator files between steps.
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
from app.services.fleet_autonomy import (
    AUTONOMY_LEVELS,
    effective_gates,
    gate_catalog,
    gated_tool_rules,
    make_fleet_autonomy_gate,
)
from app.services.fleet_docs import clean_structured
from app.tools.base import HumanInputRequestedError

_VALIDATION_FLEETS = ("ctem-loop", "ctem-vuln-prioritization", "ctem-ai-security")
_SCENARIO = "11111111-1111-4111-8111-111111111111"
_TEST = "33333333-3333-4333-8333-333333333333"
_ASSET = "44444444-4444-4444-8444-444444444444"

#: Every call that creates or starts a validation: offered and run, never paused.
_VALIDATION_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("create_openaev_scenario", {"name": "Reproduce the threat"}),
    ("import_openaev_scenario", {"file_id": "f"}),
    ("manage_openaev_scenario", {"action": "launch", "scenario_id": _SCENARIO}),
    (
        "manage_openaev_scenario",
        {"action": "schedule_recurrence", "scenario_id": _SCENARIO, "recurrence_cron": "0 0 2 * * *"},
    ),
    ("manage_openaev_simulation", {"action": "status", "exercise_id": _SCENARIO, "status": "RUNNING"}),
    ("add_openaev_inject_to_scenario", {"scenario_id": _SCENARIO, "title": "t", "injector_contract_id": "c"}),
    ("manage_openaev_inject", {"action": "trigger", "scope": "simulation", "parent_id": _SCENARIO}),
    ("create_openaev_atomic_testing", {"title": "t", "injector_contract_id": "c", "asset_ids": [_ASSET]}),
    ("manage_openaev_atomic_testing", {"action": "launch", "inject_id": _TEST}),
    ("manage_opencti_security_coverage", {"action": "create", "covered_id": "x"}),
    ("create_jira_issue", {"summary": "fix"}),
    ("sync_ctem_case_ticket", {"create": "true"}),
]


def _gates(slug: str) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = BUILTIN_FLEET_STRUCTURED[slug]["gates"]
    return gates


def _pauses(gate: Any, call: Any) -> bool:
    try:
        gate(call)
    except HumanInputRequestedError:
        return True
    return False


@pytest.mark.parametrize("slug", _VALIDATION_FLEETS)
def test_two_gates_wait_at_the_seeded_level_none_autonomous_all_at_full_gates(slug: str) -> None:
    def waiting(level: str) -> set[str]:
        return {e["key"] for e in gate_catalog(level, _gates(slug)) if e["gated"]}

    assert waiting(SEEDED_FLEET_AUTONOMY_LEVEL) == {CTEM_VALIDATION_GATE, CTEM_MOBILIZE_GATE}
    assert waiting("autonomous") == set()
    assert waiting("full_gates") == {g["key"] for g in _gates(slug)}


def test_the_third_party_fleet_validation_gate_does_not_wait() -> None:
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


@pytest.mark.parametrize("slug", _VALIDATION_FLEETS)
@pytest.mark.parametrize(("name", "arguments"), _VALIDATION_CALLS)
def test_a_seeded_gate_never_pauses_a_tool_call(slug: str, name: str, arguments: dict[str, Any]) -> None:
    """#1675: the tool runs; the wait is a decision item, never a pause inside a run."""
    for level in AUTONOMY_LEVELS:
        gate = make_fleet_autonomy_gate(None, gated_tool_rules(level, _gates(slug)))
        assert not _pauses(gate, SimpleNamespace(id="c1", name=name, arguments=arguments)), (level, name)


def test_the_stored_catalog_carries_no_tools_and_no_grant() -> None:
    cleaned = clean_structured({"gates": _gates("ctem-loop")}) or {}
    for gate in cleaned["gates"]:
        assert not gate.get("tools") and not gate.get("grant"), gate["key"]


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
