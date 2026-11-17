"""The Discovery stage page's mixed views (#11998): techniques per ATT&CK tactic read on
the T-code walk, the top threats and the sectors and countries they target, and the
viz payload that carries each list only when OpenCTI answered it.

No DB / network: ``_graphql`` is monkeypatched.
"""

from __future__ import annotations

from app.api.v1 import ctem_metrics
from app.services import ctem_metrics_collector as collector


def _node(code: str, *phases: str) -> dict:
    return {
        "node": {
            "x_mitre_id": code,
            "killChainPhases": [{"kill_chain_name": "mitre-attack", "phase_name": p} for p in phases]
            + [{"kill_chain_name": "other-chain", "phase_name": "ignored"}],
        }
    }


async def test_tcode_walk_counts_distinct_techniques_per_tactic(monkeypatch):
    pages = [
        {"stixCoreObjects": {"pageInfo": {"endCursor": "c1", "hasNextPage": True},
                             "edges": [_node("T1", "execution"), _node("T2", "execution", "persistence")]}},
        # T1 again on the next page: counted once.
        {"stixCoreObjects": {"pageInfo": {"hasNextPage": False},
                             "edges": [_node("T1", "execution"), _node("T3", "discovery")]}},
    ]

    async def fake_graphql(client, base_url, headers, query, variables=None):
        return pages.pop(0), None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    tactics: dict[str, int] = {}
    codes = await collector._octi_threat_tcodes(None, "http://x", {}, tactics)

    assert codes == ["T1", "T2", "T3"]
    assert tactics == {"execution": 2, "persistence": 1, "discovery": 1}
    assert "ignored" not in tactics


async def test_tcode_walk_reads_each_tactic_order_from_opencti(monkeypatch):
    def node(code: str, phase: str, order: int | None) -> dict:
        kcp = {"kill_chain_name": "mitre-attack", "phase_name": phase}
        if order is not None:
            kcp["x_opencti_order"] = order
        return {"node": {"x_mitre_id": code, "killChainPhases": [kcp]}}

    pages = [
        {
            "stixCoreObjects": {
                "pageInfo": {"hasNextPage": False},
                "edges": [node("T1", "impact", 40), node("T2", "execution", 4), node("T3", "custom", None)],
            }
        }
    ]

    async def fake_graphql(client, base_url, headers, query, variables=None):
        return pages.pop(0), None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    tactics: dict[str, int] = {}
    order: dict[str, int] = {}
    await collector._octi_threat_tcodes(None, "http://x", {}, tactics, order)

    assert order == {"impact": 40, "execution": 4}
    assert tactics == {"impact": 1, "execution": 1, "custom": 1}


async def test_tcode_walk_without_tactics_is_unchanged(monkeypatch):
    async def fake_graphql(client, base_url, headers, query, variables=None):
        return {"stixCoreObjects": {"pageInfo": {"hasNextPage": False}, "edges": [_node("T9", "impact")]}}, None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    assert await collector._octi_threat_tcodes(None, "http://x", {}) == ["T9"]


async def test_discovery_tops_names_rows_and_drops_unnamed(monkeypatch):
    async def fake_graphql(client, base_url, headers, query, variables=None):
        assert query is collector._OCTI_DISCOVERY_TOPS
        assert variables == {"threats": collector._THREAT_TYPES}
        return {
            "topThreats": [
                {"label": "a", "value": 142, "entity": {"name": "APT29"}},
                {"label": "b", "value": 3, "entity": None},
            ],
            "sectors": [{"label": "s", "value": 12, "entity": {"name": "Technology"}}],
            "countries": [{"label": "c", "value": 0, "entity": {"name": "France"}}],
        }, None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    out = await collector._octi_discovery_tops(None, "http://x", {})

    assert out["top_threats"] == [{"name": "APT29", "value": 142}]
    assert out["target_sectors"] == [{"name": "Technology", "value": 12}]
    # A zero is not a measure of targeting: dropped, the card then shows its empty state.
    assert out["target_countries"] == []


async def test_discovery_tops_error_is_empty(monkeypatch):
    async def fake_graphql(client, base_url, headers, query, variables=None):
        return None, "boom"

    monkeypatch.setattr(collector, "_graphql", fake_graphql)
    assert await collector._octi_discovery_tops(None, "http://x", {}) == {}


def test_discovery_viz_carries_only_answered_lists():
    facts = {
        "techniques_by_tactic": [{"tactic": "execution", "count": 3}],
        "top_threats": [{"name": "APT29", "value": 142}],
        "target_sectors": [],
        "threat_techniques": 963,
    }
    viz = ctem_metrics._stage_viz("discovery", facts, "cyber")
    assert viz == {
        "techniques_by_tactic": [{"tactic": "execution", "count": 3}],
        "top_threats": [{"name": "APT29", "value": 142}],
    }
    # OpenAEV only (no OpenCTI facts): nothing, the page draws its gap filler.
    assert ctem_metrics._stage_viz("discovery", {"threat_informed_coverage_pct": 40}, "cyber") == {}
    # Another vertical keeps its empty payload.
    assert ctem_metrics._stage_viz("discovery", facts, "fimi") == {}
