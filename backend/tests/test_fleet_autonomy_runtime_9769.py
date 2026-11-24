"""Fleet autonomy level at run time (#9769, slice S1b): the context block and the enforcement in the tool path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.services.fleet_autonomy import (
    AUTONOMY_LEVELS,
    FLEET_AUTONOMY_HEADER,
    _structured_gates,
    fleet_autonomy_block,
    gated_tool_rules,
    is_gated,
    make_fleet_autonomy_gate,
    rule_matches,
)
from app.services.fleet_docs import clean_structured
from app.tools.base import HumanInputRequestedError

_CTEM = sorted(BUILTIN_FLEET_STRUCTURED)

_LAUNCH = SimpleNamespace(
    id="c-launch", name="manage_openaev_simulation", arguments={"action": "status", "exercise_id": "x", "status": "RUNNING"}
)
_READ = SimpleNamespace(id="c-read", name="manage_openaev_simulation", arguments={"action": "get", "exercise_id": "x"})
_TICKET = SimpleNamespace(id="c-ticket", name="create_jira_issue", arguments={"summary": "fix"})


def _gates(slug: str) -> list[dict]:
    return BUILTIN_FLEET_STRUCTURED[slug].get("gates", [])


def _paused(gate, call) -> bool:
    try:
        gate(call)
    except HumanInputRequestedError:
        return True
    return False


@pytest.mark.parametrize("slug", _CTEM)
def test_every_seeded_gate_still_validates_with_its_tools(slug):
    cleaned = clean_structured(BUILTIN_FLEET_STRUCTURED[slug])
    assert cleaned is not None
    assert [g.get("tools", []) for g in cleaned.get("gates", [])] == [g.get("tools", []) for g in _gates(slug)]


@pytest.mark.parametrize("slug", _CTEM)
@pytest.mark.parametrize("level", AUTONOMY_LEVELS)
def test_rules_follow_the_level_matrix_on_each_ctem_fleet(slug, level):
    rules = gated_tool_rules(level, _gates(slug))
    expected = {
        (gate["key"], tool["name"])
        for gate in _gates(slug)
        if is_gated(level, gate.get("impact"))
        for tool in gate.get("tools", [])
    }
    assert {(r["key"], r["name"]) for r in rules} == expected


@pytest.mark.parametrize("slug", _CTEM)
def test_no_gate_bypass_at_full_human_gates(slug):
    """At the default level every declared tool of every gate pauses."""
    rules = gated_tool_rules("full_gates", _gates(slug))
    declared = [(g["key"], t["name"]) for g in _gates(slug) for t in g.get("tools", [])]
    assert [(r["key"], r["name"]) for r in rules] == declared


def test_an_unknown_level_reads_as_full_human_gates():
    gates = [g for slug in _CTEM for g in _gates(slug)]
    assert gated_tool_rules("bogus", gates) == gated_tool_rules("full_gates", gates)


def test_a_seeded_simulation_launch_and_a_read_never_pause() -> None:
    """#1675: the validation is a decision item; the launch call itself runs at every level."""
    gates = [g for g in _gates("ctem-loop") if g.get("key") == "ctem.validation.non_readonly"]
    assert gates, "the threat response fleet declares the validation gate"
    for level in AUTONOMY_LEVELS:
        gate = make_fleet_autonomy_gate(None, gated_tool_rules(level, gates))
        assert _paused(gate, _LAUNCH) is False
        assert _paused(gate, _READ) is False


def test_a_seeded_ticket_never_pauses_on_the_mobilize_gate() -> None:
    gates = [g for g in _gates("ctem-vuln-prioritization") if g.get("key") == "ctem.mobilize"]
    assert gates
    for level in AUTONOMY_LEVELS:
        gate = make_fleet_autonomy_gate(None, gated_tool_rules(level, gates))
        assert _paused(gate, _TICKET) is False


def test_an_approved_call_runs_and_the_inner_gate_still_applies():
    rules = [{"name": "create_jira_issue", "when": {}, "key": "ticket.create", "action": "Create a ticket"}]
    seen: list[str] = []
    gate = make_fleet_autonomy_gate(lambda call: seen.append(call.id), rules, approved_call_ids={"c-ticket"})
    gate(_TICKET)
    assert seen == ["c-ticket"]
    other = SimpleNamespace(id="c-other", name="create_jira_issue", arguments={})
    assert _paused(gate, other)
    assert seen == ["c-ticket"]  # the fleet gate paused before the inner one ran


def test_rule_matching_is_case_insensitive_and_fails_closed_on_malformed_arguments():
    rule = {"name": "manage_openaev_simulation", "when": {"action": ["status"], "status": ["RUNNING"]}}
    assert rule_matches(rule, "manage_openaev_simulation", {"action": "status", "status": "running"})
    assert not rule_matches(rule, "manage_openaev_simulation", {"action": "status", "status": "PAUSED"})
    assert not rule_matches(rule, "manage_openaev_simulation", {"action": "status"})
    assert rule_matches(rule, "manage_openaev_simulation", "not a mapping")
    assert not rule_matches(rule, "manage_openaev_scenario", {"action": "status", "status": "RUNNING"})


def test_the_context_block_names_the_level_and_each_gate():
    gates = _gates(_CTEM[0])
    block = fleet_autonomy_block("medium", gates)
    assert FLEET_AUTONOMY_HEADER in block
    assert "Medium (medium)" in block
    for gate in gates:
        assert gate["action"] in block
    assert fleet_autonomy_block("bogus", []).count("Full human gates") == 1


def test_the_block_says_gates_are_instructions_unless_the_fleet_opted_in() -> None:
    """#1467: no tool-level pause by default; an answered decision is not asked again."""
    gates = _gates(_CTEM[0])
    plain = fleet_autonomy_block("low", gates)
    assert "pauses the tool calls" not in plain
    assert "without asking again" in plain
    enforced = fleet_autonomy_block("low", gates, enforced=True)
    assert "platform also pauses the tool calls" in enforced


def test_structured_gates_are_read_from_the_stored_documentation_only():
    assert _structured_gates(None) == []
    assert _structured_gates({"sections": {}}) == []
    assert _structured_gates({"structured": {"gates": "x"}}) == []
    assert _structured_gates({"structured": {"gates": [{"key": "a"}, "x"]}}) == [{"key": "a"}]
