"""CTEM loop gate catalog (#916, CTEM-34): one human decision out of the box, every gate configurable.

Pinned here without a database:
- every CTEM loop fleet declares the seven CTEM gate keys, in loop order;
- at the seeded level (Low) exactly one gate waits, ``ctem.mobilize``; Full
  human gates makes every gate wait; Full autonomous none;
- the remediation ticket tools are the Mobilize action (one decision covers the
  tickets of the case), the OpenAEV launches are the validation gate;
- per-gate pins are writable on a built-in fleet and the seed never writes
  them, so a pin survives a seed upgrade;
- no CTEM persona hard-codes an approval: they apply the effective catalog.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED, CTEM_GATE_KEYS, CTEM_MOBILIZE_GATE
from app.agents.fleet_seed import SEEDED_FLEET_AUTONOMY_LEVEL, _seed_approval_gates
from app.agents.seed import SEED_DATA_DIR
from app.services.fleet_autonomy import AUTONOMY_LEVELS, effective_gates, gate_catalog, gated_tool_rules
from app.services.fleets import BUILTIN_FLEET_WRITABLE_FIELDS, ensure_builtin_fleet_writable

_LOOP_FLEETS = ("ctem-threat-response", "ctem-vuln-prioritization", "ctem-ai-security", "ctem-third-party-risk")
_TICKET_TOOLS = {"create_jira_issue", "create_servicenow_incident", "create_servicenow_change", "sync_ctem_case_ticket"}


def _gates(slug: str) -> list[dict]:
    return BUILTIN_FLEET_STRUCTURED[slug]["gates"]


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_every_loop_fleet_declares_the_ctem_gate_catalog_in_loop_order(slug):
    assert tuple(g["key"] for g in _gates(slug)) == CTEM_GATE_KEYS


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_out_of_the_box_only_mobilize_waits(slug):
    catalog = gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, _gates(slug))
    assert [e["key"] for e in catalog if e["gated"]] == [CTEM_MOBILIZE_GATE]


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_full_gates_waits_on_every_gate_and_autonomous_on_none(slug):
    assert all(e["gated"] for e in gate_catalog("full_gates", _gates(slug)))
    assert not any(e["gated"] for e in gate_catalog("autonomous", _gates(slug)))


def test_one_mobilize_decision_covers_the_tickets_of_its_case():
    for slug in ("ctem-threat-response", "ctem-vuln-prioritization"):
        mobilize = next(g for g in _gates(slug) if g["key"] == CTEM_MOBILIZE_GATE)
        assert {t["name"] for t in mobilize["tools"]} == _TICKET_TOOLS
        rules = gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, _gates(slug))
        assert {r["key"] for r in rules if r["name"] in _TICKET_TOOLS} == {CTEM_MOBILIZE_GATE}


def test_no_former_tool_gate_key_is_left():
    former = {"ticket.create", "openaev.scenario.launch", "openaev.validation.launch", "openaev.ai_test.launch"}
    former.add("supplier.action")
    keys = {g["key"] for slug in _LOOP_FLEETS for g in _gates(slug)}
    assert not keys & former


@pytest.mark.parametrize("level", AUTONOMY_LEVELS)
def test_a_pin_moves_one_gate_either_way_at_every_level(level):
    gates = effective_gates(
        _gates("ctem-vuln-prioritization"),
        None,
        pins={CTEM_MOBILIZE_GATE: "run", "ctem.remediation.decision": "wait"},
    )
    catalog = {e["key"]: e["gated"] for e in gate_catalog(level, gates)}
    assert catalog[CTEM_MOBILIZE_GATE] is False
    assert catalog["ctem.remediation.decision"] is True


def test_pins_are_writable_on_a_built_in_fleet():
    assert "approval_pins" in BUILTIN_FLEET_WRITABLE_FIELDS
    fleet = SimpleNamespace(is_builtin=True, slug="ctem-vuln-prioritization")
    ensure_builtin_fleet_writable(fleet, {"autonomy_level", "approval_pins"})


def test_a_pin_survives_a_seed_upgrade():
    fleet = SimpleNamespace(approval_gates=None, approval_pins={CTEM_MOBILIZE_GATE: "run"})
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"])
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"])
    assert fleet.approval_pins == {CTEM_MOBILIZE_GATE: "run"}
    assert [g["key"] for g in fleet.approval_gates] == list(CTEM_GATE_KEYS)


_ADDED = {
    "key": "customer.production_window",
    "action": "Change production outside the change window",
    "module": "XTM One",
    "approver": "Change manager",
    "impact": "high",
    "tools": [{"name": "create_servicenow_change"}],
}


def test_added_gates_are_writable_on_a_built_in_fleet() -> None:
    assert "customer_gates" in BUILTIN_FLEET_WRITABLE_FIELDS
    fleet: Any = SimpleNamespace(is_builtin=True, slug="ctem-vuln-prioritization")
    ensure_builtin_fleet_writable(fleet, {"customer_gates"})


def test_an_added_gate_joins_the_seeded_catalog_and_the_level_acts_on_it() -> None:
    seeded = _gates("ctem-vuln-prioritization")
    gates = effective_gates(seeded, None, customer=[_ADDED])
    assert [g["key"] for g in gates] == [*CTEM_GATE_KEYS, _ADDED["key"]]
    assert gates[-1]["source"] == "customer"
    catalog = {e["key"]: e["gated"] for e in gate_catalog("full_gates", gates)}
    assert catalog[_ADDED["key"]] is True
    pinned = effective_gates(seeded, None, pins={_ADDED["key"]: "run"}, customer=[_ADDED])
    assert {e["key"]: e["gated"] for e in gate_catalog("full_gates", pinned)}[_ADDED["key"]] is False


def test_an_added_gate_never_replaces_a_seeded_one() -> None:
    clash = dict(_ADDED, key=CTEM_MOBILIZE_GATE, tools=[])
    gates = effective_gates(_gates("ctem-vuln-prioritization"), None, customer=[clash, {"no": "key"}, "x"])
    mobilize = [g for g in gates if g["key"] == CTEM_MOBILIZE_GATE]
    assert len(mobilize) == 1 and mobilize[0]["source"] == "stored" and mobilize[0]["tools"]
    assert len(gates) == len(CTEM_GATE_KEYS)


def test_an_added_gate_survives_a_seed_upgrade() -> None:
    fleet: Any = SimpleNamespace(approval_gates=None, approval_pins=None, customer_gates=[_ADDED])
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"])
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"])
    assert fleet.customer_gates == [_ADDED]
    keys = [g["key"] for g in effective_gates(fleet.approval_gates, None, customer=fleet.customer_gates)]
    assert keys[-1] == _ADDED["key"]


_PERSONAS = (
    "ctem-threat-response-orchestrator.md",
    "ctem-vuln-prioritization-orchestrator.md",
    "ctem-ai-security-orchestrator.md",
    "ctem-third-party-risk-orchestrator.md",
    "ctem-remediation-coordinator.md",
)
_HARD_CODED = re.compile(
    r"(?i)(never launch without|behind approval|waits for a human|goes through a `?needs_human`? work item"
    r"|is a work item `ctem:f\d:[a-z]+:` followed by [^.]* with `needs_human: true`)"
)


@pytest.mark.parametrize("name", _PERSONAS)
def test_no_ctem_persona_hard_codes_an_approval(name):
    text = (SEED_DATA_DIR / "agent_persona" / name).read_text(encoding="utf-8")
    assert not _HARD_CODED.search(text), name
