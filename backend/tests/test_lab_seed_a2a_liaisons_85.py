"""The A2A liaisons seed of the lab (#85): partner nodes on agents the fleet graphs draw."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _tree() -> ast.Module:
    return ast.parse(LAB.read_text(encoding="utf-8"))


def _source(name: str) -> str:
    return ast.unparse(next(n for n in _tree().body if isinstance(n, ast.FunctionDef) and n.name == name))


def _constant(name: str):
    for node in _tree().body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module constant of lab.py")


def test_the_cards_are_on_a_reserved_host():
    urls = _constant("A2A_LIAISON_CARD_URLS")
    assert len(urls) >= 2
    assert all(urlsplit(u).hostname.endswith(".invalid") for u in urls)


def test_the_bound_agents_are_the_ones_the_fleet_seeds_register():
    names = _constant("A2A_LIAISON_AGENTS")
    for name, seed in (("flow orchestrator", "seed_fleet_flow_states"), ("large lead", "seed_large_fleet")):
        assert name in names
        assert f"LAB_AGENTS['{name}']" in _source(seed), seed


def test_the_seed_creates_both_directions_binds_reads_back_and_logs():
    helper = _source("seed_a2a_liaisons")
    for part in (
        "'a2a_direction': 'inbound'",
        "'a2a_direction': 'outbound'",
        "'card_urls'",
        "/a2a-credential",
        "'integration_bindings': {'oauth_a2a': [inbound, outbound]}",
        "'/a2a/liaisons'",
        "seeded A2A liaisons",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed_after_the_fleets():
    seed = _source("seed_entities")
    assert "seed_a2a_liaisons(token, base)" in seed
    assert "skipped['A2A liaisons']" in seed
    position = seed.index("seed_a2a_liaisons(token, base)")
    assert seed.index("seed_large_fleet(token, base)") < position
    assert seed.index("seed_fleet_flow_states(") < position


def test_the_seeded_shape_draws_the_edges_the_read_back_expects():
    """The product's own builder, fed the shape the seed writes, draws what the seed reads back."""
    from app.a2a.liaisons import LiaisonAgent, LiaisonIntegration, LiaisonTarget, build_liaisons

    urls = _constant("A2A_LIAISON_CARD_URLS")
    inbound, outbound = uuid.uuid4(), uuid.uuid4()
    agents = [
        LiaisonAgent(id=uuid.uuid4(), integration_bindings={"oauth_a2a": [str(inbound), str(outbound)]})
        for _ in _constant("A2A_LIAISON_AGENTS")
    ]
    out = build_liaisons(
        agents,
        [
            LiaisonIntegration(id=inbound, name="intake", provider="oauth_a2a"),
            LiaisonIntegration(id=outbound, name="delegate", provider="oauth_a2a"),
        ],
        {inbound: "inbound", outbound: "outbound"},
        [LiaisonTarget(id=uuid.uuid4(), integration_id=outbound, card_url=u) for u in urls],
    )
    for agent in agents:
        agent_id = str(agent.id)
        assert [e for e in out.edges if e.target_id == agent_id and e.direction == "inbound"]
        assert len([e for e in out.edges if e.source_id == agent_id and e.direction == "outbound"]) == len(urls)
    assert len(out.partners) == 1 + len(urls)
