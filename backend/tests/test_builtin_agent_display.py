"""Display data of the built-in agent seed: icons that resolve, names that fit.

The Agentic Flow map, the agent lists and the chat attribution line render an
agent's ``icon`` through ``ICON_MAP`` (frontend/src/lib/constants.ts); a name
missing there falls back to the generic Bot on every surface. Seeded names
are drawn on the map under a pixel budget: a name over the lone-node budget is
cut on every connected node. A name within it can still be cut on a crowded
row, where the layout gives the node less room: that is the layout slice (slice
2 of #9588), not this test.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS, CTEM_VULN_PRIORITIZATION_FLEET

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
_CONSTANTS = _FRONTEND / "lib" / "constants.ts"
_AGENT_FLOW = _FRONTEND / "components" / "ui" / "agent-flow.tsx"

# Characters a name keeps on a fleet graph node with no neighbour on its row:
# floor((LONE_NAME_ROOM_PX - LABEL_GAP_PX) / (NAME_PX * GLYPH_ADVANCE)), the
# formula ``fittedNameLines`` applies in agent-flow.tsx. Pinned here and read
# back from the renderer below. This is the lone-node best case: a node with a
# neighbour on its row gets a smaller ``nameRoomPx``, so a name within the
# budget can still be cut on a crowded row. Those cuts come from the layout,
# which slice 2 of #9588 owns; this test only proves no name is cut everywhere.
# Never raise it to make a name pass: shorten the name.
_NAME_BUDGET = 24

# Fleet graph nodes still over the lone-node budget, tracked for slice 2 of
# #9588 (the map layout slice): each one is cut on the graph wherever it sits.
# Nodes within the budget but cut on a crowded row are not listed here: they
# are slice 2 acceptance items on #9588. Remove an entry when
# its name is shortened or the layout gives it room; the test fails on an entry
# that already fits, so the list only shrinks.
_SLICE_2_EXCEPTIONS = {
    "ctem-prioritization-analyst",
    "ctem-mobilization-analyst",
    "exposure-briefing-analyst",
    "opencti-threat-hunt-creator",
    "opencti-detection-rule-converter",
    "opencti-dashboard-widget-creator",
    # Connected to the PIR desk (#10542); a pre-existing agent, never renamed by a fleet.
    "opencti-dashboard-creator",
    # Connected to the detection validation desk (#10575); pre-existing agents, never renamed by a fleet.
    "openaev-scenario-publisher",
    "remediation-splunk-rule-generator",
    "remediation-crowdstrike-rule-generator",
    "aev-detection-rules-generator",
    "cti-ttp-harvester-filigran-ai",
}


def _ts_number(source: str, name: str) -> float:
    match = re.search(rf"^const {name} = ([0-9.]+);", source, re.M)
    assert match, f"{name} is no longer a numeric constant in agent-flow.tsx"
    return float(match.group(1))


def _icon_map_keys() -> set[str]:
    source = _CONSTANTS.read_text(encoding="utf-8")
    block = re.search(r"export const ICON_MAP[^{]*\{(.*?)\n\};", source, re.S)
    assert block, "ICON_MAP is no longer a plain object literal in constants.ts"
    return set(re.findall(r"^\s*([A-Za-z0-9]+),", block.group(1), re.M))


def test_every_seeded_agent_icon_resolves_in_icon_map():
    keys = _icon_map_keys()
    missing = sorted(
        {a.get("icon", "Bot") for a in BUILTIN_AGENTS if a.get("icon", "Bot") not in keys}
    )
    assert not missing, f"seeded icons render the Bot fallback: {missing}"


def test_every_seeded_fleet_icon_resolves_in_icon_map():
    keys = _icon_map_keys()
    fleets = [*BUILTIN_FLEETS, CTEM_VULN_PRIORITIZATION_FLEET]
    missing = sorted({f["icon"] for f in fleets if f.get("icon") and f["icon"] not in keys})
    assert not missing, f"seeded fleet icons render the Bot fallback: {missing}"


def test_name_budget_matches_the_map_renderer():
    source = _AGENT_FLOW.read_text(encoding="utf-8")
    room = _ts_number(source, "LONE_NAME_ROOM_PX") - _ts_number(source, "LABEL_GAP_PX")
    per_char = _ts_number(source, "NAME_PX") * _ts_number(source, "GLYPH_ADVANCE")
    assert math.floor(room / per_char) == _NAME_BUDGET


def _fleet_node_slugs() -> set[str]:
    slugs: set[str] = set()
    for fleet in [*BUILTIN_FLEETS, CTEM_VULN_PRIORITIZATION_FLEET]:
        slugs.add(fleet["orchestrator"])
        slugs.update(m["agent"] for m in fleet.get("members", []))
        slugs.update(c["agent"] for c in fleet.get("connected", []))
        slugs.update(h["from"] for h in fleet.get("entry_handovers", []))
    return slugs


def test_seeded_fleet_node_names_fit_the_map_label():
    names = {a["slug"]: a["name"] for a in BUILTIN_AGENTS}
    nodes = {slug for slug in _fleet_node_slugs() if slug in names}
    long = sorted(
        names[slug] for slug in nodes - _SLICE_2_EXCEPTIONS if len(names[slug]) > _NAME_BUDGET
    )
    assert not long, f"fleet graph names over {_NAME_BUDGET} characters (cut on the map): {long}"
    fitting = sorted(
        slug for slug in _SLICE_2_EXCEPTIONS if slug in names and len(names[slug]) <= _NAME_BUDGET
    )
    assert not fitting, f"slice 2 exceptions that now fit, remove them: {fitting}"
    stale = sorted(_SLICE_2_EXCEPTIONS - nodes)
    assert not stale, f"slice 2 exceptions no longer on a fleet graph: {stale}"


def test_fleet_members_carry_no_product_prefix():
    """Inside a fleet the hull names the program: a CTEM, AEV or Remediation
    prefix on a fleet agent only lengthens its label."""
    # The five stage analysts name the CTEM stage itself; the AEV Detection
    # Rules Generator is the OpenAEV chain entry a handover test names; the
    # CTEM Assistant is the hub, not a fleet member; "Remediation" is the role
    # word of the Remediation Coordinator.
    kept = {
        "ctem-assistant",
        "ctem-remediation-coordinator",
        "ctem-scoping-analyst",
        "ctem-discovery-analyst",
        "ctem-prioritization-analyst",
        "ctem-validation-analyst",
        "ctem-mobilization-analyst",
        "aev-detection-rules-generator",
    }
    prefixed = sorted(
        a["name"]
        for a in BUILTIN_AGENTS
        if re.match(r"(CTEM|AEV|Remediation) ", a["name"]) and a["slug"] not in kept
    )
    assert not prefixed, f"product prefix on fleet agents: {prefixed}"
