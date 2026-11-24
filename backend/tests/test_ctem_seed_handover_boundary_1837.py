# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License").

"""#1837 slice 2: no seeded agent hands over into a member a built-in fleet does not expose.

``fleet_boundary_violations`` (the ``handover_not_allowed`` fleet health
finding) flags a handover from an agent outside a fleet into one of its
non-exposed members. The seeded definitions must not produce one: an outside
agent reaches a hidden role through the fleet's orchestrator.
"""

from __future__ import annotations

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS


def _violations() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for fleet in BUILTIN_FLEETS:
        home = {m["agent"] for m in fleet["members"]}
        hidden = {
            m["agent"] for m in fleet["members"] if m["agent"] != fleet["orchestrator"] and not m.get("exposed", False)
        }
        for agent in BUILTIN_AGENTS:
            if agent["slug"] in home:
                continue
            for target in agent.get("handover_agent_slugs") or []:
                if target in hidden:
                    out.append((fleet["slug"], agent["slug"], target))
    return out


def test_no_seeded_handover_into_a_hidden_fleet_member():
    assert _violations() == []


def test_entry_handovers_point_at_the_orchestrator_only():
    # The seed adds entry handovers to the fleet's orchestrator, which is always exposed.
    for fleet in BUILTIN_FLEETS:
        for entry in fleet.get("entry_handovers") or []:
            assert entry["from"] not in {m["agent"] for m in fleet["members"]}
