"""The Validation stage page's mixed views (#11998): OpenCTI's rule coverage per ATT&CK
tactic, read on the T-code walk with no extra query, its detection depth, and the viz
payload that carries each list only when OpenCTI answered it.

No DB / network: ``_graphql`` is monkeypatched.
"""

from __future__ import annotations

from app.api.v1 import ctem_metrics
from app.services import ctem_metrics_collector as collector


def _node(node_id: str, code: str, *phases: tuple[str, int]) -> dict:
    return {
        "node": {
            "id": node_id,
            "x_mitre_id": code,
            "killChainPhases": [
                {"kill_chain_name": "mitre-attack", "phase_name": p, "x_opencti_order": o} for (p, o) in phases
            ],
        }
    }


async def test_tcode_walk_collects_each_tactics_technique_ids(monkeypatch):
    pages = [
        {"stixCoreObjects": {"pageInfo": {"endCursor": "c1", "hasNextPage": True},
                             "edges": [_node("a", "T1", ("execution", 4)), _node("b", "T2", ("execution", 4), ("persistence", 5))]}},
        # The same technique on the next page: collected once.
        {"stixCoreObjects": {"pageInfo": {"hasNextPage": False}, "edges": [_node("a", "T1", ("execution", 4))]}},
    ]

    async def fake_graphql(client, base_url, headers, query, variables=None):
        return pages.pop(0), None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    ids: dict[str, set[str]] = {}
    await collector._octi_threat_tcodes(None, "http://x", {}, {}, {}, ids)

    assert ids == {"execution": {"a", "b"}, "persistence": {"b"}}


def test_rule_coverage_by_tactic_counts_the_covered_ids_in_kill_chain_order():
    rows = collector._rule_coverage_by_tactic(
        {"persistence": {"b"}, "execution": {"a", "b", "c"}, "custom": {"d"}, "empty": set()},
        {"execution": 4, "persistence": 5},
        {"a", "b", "z"},
    )
    assert rows == [
        {"tactic": "execution", "total": 3, "covered": 2},
        {"tactic": "persistence", "total": 1, "covered": 1},
        # A tactic without an order comes last; an empty one is dropped.
        {"tactic": "custom", "total": 1, "covered": 0},
    ]


def test_validation_viz_carries_the_opencti_lists_only_when_answered():
    facts = {
        "rule_coverage_by_tactic": [{"tactic": "execution", "total": 3, "covered": 2}],
        "detection_depth": [{"name": "indicators", "value": 12}],
    }
    viz = ctem_metrics._stage_viz("validation", facts, "cyber")
    assert viz["rule_coverage_by_tactic"] == facts["rule_coverage_by_tactic"]
    assert viz["detection_depth"] == facts["detection_depth"]
    # An unanswered list is absent, never an empty frame.
    assert ctem_metrics._stage_viz("validation", {"rule_coverage_by_tactic": []}, "cyber") == {}
    # Another vertical reads no OpenCTI list on Validation.
    assert "rule_coverage_by_tactic" not in ctem_metrics._stage_viz("validation", facts, "fraud")
