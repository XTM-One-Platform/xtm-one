"""The Attack Flow widget's data: a MITRE Attack Flow bundle read into the graph shape, and a kill chain's
techniques laid out stage by stage by the ``flow`` step (an OpenCTI list giving their ranked tactics)."""

from __future__ import annotations

from app.services.dashboard_live import closed_static_data
from app.services.dashboard_live import opencti as live_opencti
from app.services.dashboard_pipeline import FlowStep, run_pipeline

BUNDLE = {
    "type": "bundle",
    "id": "bundle--1",
    "objects": [
        {"type": "attack-flow", "id": "attack-flow--1", "name": "Operation Dream Job", "start_refs": ["attack-action--1"]},
        {
            "type": "attack-action", "id": "attack-action--1", "name": "Spearphishing Link", "technique_id": "T1566.002",
            "tactic_id": "TA0001", "effect_refs": ["attack-condition--1"],
        },
        {"type": "attack-condition", "id": "attack-condition--1", "description": "Macro enabled",
         "on_true_refs": ["attack-operator--1"], "on_false_refs": ["attack-action--3"]},
        {"type": "attack-operator", "id": "attack-operator--1", "operator": "and", "effect_refs": ["attack-action--2"]},
        {"type": "attack-action", "id": "attack-action--2", "technique_ref": "attack-pattern--1", "tactic_ref": "x-mitre-tactic--1",
         "asset_refs": ["attack-asset--1"]},
        {"type": "attack-action", "id": "attack-action--3", "name": "Give up", "effect_refs": ["attack-action--missing"]},
        {"type": "attack-asset", "id": "attack-asset--1", "name": "Workstation"},
        {"type": "attack-pattern", "id": "attack-pattern--1", "name": "User Execution",
         "external_references": [{"source_name": "mitre-attack", "external_id": "T1204"}]},
        {"type": "x-mitre-tactic", "id": "x-mitre-tactic--1", "name": "Execution"},
        {"type": "identity", "id": "identity--1", "name": "Ignored"},
    ],
}  # fmt: skip


def test_an_attack_flow_bundle_is_read_as_its_graph():
    graph = closed_static_data("graph", BUNDLE)
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert set(nodes) == {"attack-flow--1", "attack-action--1", "attack-condition--1", "attack-operator--1",
                          "attack-action--2", "attack-action--3", "attack-asset--1"}  # fmt: skip
    assert nodes["attack-flow--1"]["kind"] == "start"
    assert nodes["attack-action--1"].items() >= {"label": "Spearphishing Link", "kind": "action", "technique": "T1566.002", "tactic": "TA0001"}.items()
    # An action naming its technique and tactic by reference reads their names.
    assert nodes["attack-action--2"].items() >= {"label": "User Execution", "technique": "T1204", "tactic": "Execution"}.items()
    assert nodes["attack-operator--1"].items() >= {"label": "AND", "kind": "operator", "operator": "AND"}.items()
    edges = {(e["source"], e["target"], e["label"]) for e in graph["edges"]}
    assert ("attack-condition--1", "attack-operator--1", "true") in edges
    assert ("attack-condition--1", "attack-action--3", "false") in edges
    assert ("attack-action--2", "attack-asset--1", None) in edges
    assert ("attack-flow--1", "attack-action--1", None) in edges
    # A reference to nothing in the bundle draws no edge.
    assert not any(target == "attack-action--missing" for _, target, _ in edges)


def test_a_flow_node_keeps_only_its_closed_details():
    graph = closed_static_data(
        "graph",
        {"nodes": [{"id": "x", "label": "X", "kind": "exploit", "operator": "XOR", "technique": "T" * 50, "secret": "s"}], "edges": []},
    )
    assert graph["nodes"] == [{"id": "x", "label": "X", "group": None, "value": None, "technique": "T" * 20}]


def test_a_flow_step_lays_out_techniques_by_ranked_tactic_joined_by_or():
    rows = {
        "columns": [{"key": "name", "label": "Name"}, {"key": "x_mitre_id", "label": "ID"}, {"key": "killChainPhases", "label": "Tactics", "kind": "chips"}],
        "rows": [
            {"name": "Phishing", "x_mitre_id": "T1566", "killChainPhases": [{"label": "initial-access", "rank": 3}]},
            {"name": "PowerShell", "x_mitre_id": "T1059.001", "killChainPhases": [{"label": "initial-access", "rank": 3}, {"label": "execution", "rank": 4}]},
            {"name": "Scanning", "x_mitre_id": "T1595", "killChainPhases": [{"label": "reconnaissance", "rank": 1}]},
        ],
    }  # fmt: skip
    shape, graph = run_pipeline([FlowStep(op="flow", stage="killChainPhases", label="name", technique="x_mitre_id")], "rows", rows)
    assert shape == "graph"
    actions = [(n["tactic"], n["label"], n["technique"]) for n in graph["nodes"] if n.get("kind") == "action"]
    assert actions == [
        ("reconnaissance", "Scanning", "T1595"),
        ("initial-access", "Phishing", "T1566"),
        ("initial-access", "PowerShell", "T1059.001"),
        ("execution", "PowerShell", "T1059.001"),
    ]
    joins = [n["id"] for n in graph["nodes"] if n.get("kind") == "operator"]
    assert len(joins) == 2
    into_second = {e["source"] for e in graph["edges"] if e["target"] == joins[0]}
    assert into_second == {"s0-0"}


def test_a_flow_orders_tactics_within_each_kill_chain_never_across():
    """Two kill chains number their tactics each their own way (a mobile one starts at initial-access 0, the
    enterprise one at reconnaissance 0): mixing their numbers put initial-access before reconnaissance."""
    def phases(*items):
        return [{"label": label, "group": chain, "rank": rank} for label, chain, rank in items]

    rows = {
        "columns": [{"key": "name", "label": "Name"}, {"key": "k", "label": "K", "kind": "chips"}],
        "rows": [
            {"name": "Phishing", "k": phases(("initial-access", "enterprise", 2), ("initial-access", "mobile", 0))},
            {"name": "Scanning", "k": phases(("reconnaissance", "enterprise", 0))},
            {"name": "Scripting", "k": phases(("execution", "enterprise", 3), ("execution", "mobile", 1))},
            {"name": "Recon then access", "k": phases(("reconnaissance", "enterprise", 0), ("initial-access", "enterprise", 2))},
        ],
    }  # fmt: skip
    _shape, graph = run_pipeline([FlowStep(op="flow", stage="k", label="name")], "rows", rows)
    tactics = []
    for node in graph["nodes"]:
        if node.get("kind") == "action" and node["tactic"] not in tactics:
            tactics.append(node["tactic"])
    assert tactics == ["reconnaissance", "initial-access", "execution"]
    # A technique in a tactic of two chains is one action there.
    assert [n["label"] for n in graph["nodes"] if n.get("tactic") == "initial-access"] == ["Phishing", "Recon then access"]


def test_an_opencti_technique_lists_its_tactics_with_their_kill_chain_and_rank():
    node = {"killChainPhases": [
        {"kill_chain_name": "mitre-attack", "phase_name": "execution", "x_opencti_order": 3},
        {"kill_chain_name": "mitre-attack", "phase_name": "initial-access", "x_opencti_order": 2},
        {"kill_chain_name": "mitre-mobile-attack", "phase_name": "execution", "x_opencti_order": 1},
    ]}  # fmt: skip
    chips = [
        {"label": "initial-access", "group": "mitre-attack", "rank": 2},
        {"label": "execution", "group": "mitre-attack", "rank": 3},
        {"label": "execution", "group": "mitre-mobile-attack", "rank": 1},
    ]
    assert live_opencti._cell(node, "killChainPhases") == chips
    assert live_opencti._kind("killChainPhases") == "chips"
    # The rank and the chain survive the closed rows a derived source reads.
    closed = closed_static_data("rows", {"columns": [{"key": "k", "label": "K", "kind": "chips"}], "rows": [{"k": chips}]})
    assert closed["rows"][0]["k"] == chips
