"""The approval gate catalog is configuration, not documentation (#10000).

Pinned here: the seed stores each built-in fleet's declared gates (tools
included) on the fleet; a whole documentation regenerate of a built-in CTEM
fleet, whatever gates the model answers, leaves the catalog, the documented
gates and the tool rules the level enforces unchanged; a fleet with no stored
catalog keeps reading its documentation's gates (no behaviour change); a
stored empty catalog gates nothing; a fork copies the catalog.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.fleet_seed import _seed_approval_gates
from app.services import fleet_doc_generation as gen
from app.services.fleet_autonomy import AUTONOMY_LEVELS, fleet_gates, gated_tool_rules
from app.services.fleet_docs import DOC_SECTIONS, clean_structured

SLUGS = sorted(BUILTIN_FLEET_STRUCTURED)

#: What a model answers for the gates when asked (STRUCTURED_GUIDE): no key, no impact, no tools.
_MODEL_GATES = [{"action": "Approve the plan", "module": "platform", "approver": "Someone"}]


def _seeded_fleet(slug: str) -> SimpleNamespace:
    fleet = SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        name=slug,
        description="",
        skills=[],
        is_builtin=True,
        approval_gates=None,
        orchestrator_agent_id=None,
        default_context_inclusion="none",
        default_collaboration_instruction="",
        organization_scope={"mode": "primary"},
    )
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED[slug])
    shipped = clean_structured(BUILTIN_FLEET_STRUCTURED[slug])
    fleet.documentation = {
        "sections": {"purpose": "Shipped."},
        "version": 1,
        "updated_by": "seed",
        "structured": shipped,
        "shipped": {"sections": {"purpose": "Shipped."}, "structured": shipped},
    }
    return fleet


class _Model:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages, *, model, temperature, max_tokens):
        return SimpleNamespace(content=self.content, stop_reason="end_turn")


@pytest.fixture
def no_database(monkeypatch):
    from app.services import fleet_docs

    async def _context(_db, fleet):
        # The real context minus the database: the stored catalog is a computed fact.
        return {"fleet": {"name": fleet.name}, "members": [], "connected": [], **gen._gate_facts(fleet)}

    async def _fingerprint(_db, _fleet):
        return "f" * 16

    monkeypatch.setattr(gen, "load_draft_context", _context)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fingerprint)


@pytest.mark.parametrize("slug", SLUGS)
def test_the_seed_stores_every_declared_gate_with_its_tools(slug):
    fleet = _seeded_fleet(slug)
    declared = (clean_structured({"gates": BUILTIN_FLEET_STRUCTURED[slug].get("gates") or []}) or {}).get("gates") or []
    assert fleet.approval_gates == declared
    # Every declared tool rule survives the validation.
    tools = [t for g in BUILTIN_FLEET_STRUCTURED[slug].get("gates") or [] for t in g.get("tools") or []]
    assert [t for g in fleet.approval_gates for t in g.get("tools") or []] == tools, slug


@pytest.mark.parametrize("slug", SLUGS)
async def test_a_whole_regenerate_keeps_the_enforced_gates_of_a_builtin_fleet(slug, no_database):
    fleet = _seeded_fleet(slug)
    catalog_before = json.loads(json.dumps(fleet.approval_gates))
    rules_before = {
        lvl: gated_tool_rules(lvl, fleet_gates(fleet.approval_gates, fleet.documentation)) for lvl in AUTONOMY_LEVELS
    }
    answer = {
        "sections": {k: f"Text of {k}." for k in DOC_SECTIONS},
        "structured": {"purpose_line": "Regenerated.", "gates": _MODEL_GATES},
    }
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model(json.dumps(answer)))
    assert fleet.documentation["local"] is True
    assert fleet.documentation["structured"]["purpose_line"] == "Regenerated."
    # The catalog is untouched and the documentation shows it, not the model's gates.
    assert fleet.approval_gates == catalog_before
    assert fleet.documentation["structured"].get("gates", []) == catalog_before
    for lvl in AUTONOMY_LEVELS:
        assert gated_tool_rules(lvl, fleet_gates(fleet.approval_gates, fleet.documentation)) == rules_before[lvl], (
            slug,
            lvl,
        )


def test_without_a_stored_catalog_the_documentation_gates_are_read():
    documented = BUILTIN_FLEET_STRUCTURED["ctem-loop"]["gates"]
    gates = fleet_gates(None, {"sections": {}, "structured": {"gates": documented}})
    assert [g["key"] for g in gates] == [g["key"] for g in documented]


def test_a_stored_empty_catalog_gates_nothing_whatever_the_documentation_says():
    documented = BUILTIN_FLEET_STRUCTURED["ctem-loop"]["gates"]
    assert fleet_gates([], {"sections": {}, "structured": {"gates": documented}}) == []


def test_without_a_catalog_the_model_gates_are_kept_as_today():
    merged = gen.with_computed_facts({"gates": _MODEL_GATES}, {"fleet": {}})
    assert merged["gates"] == _MODEL_GATES


def test_a_stored_empty_catalog_drops_the_model_gates():
    merged = gen.with_computed_facts({"gates": _MODEL_GATES}, {"fleet": {}, "gates": []})
    assert "gates" not in merged


def test_the_context_carries_the_catalog_only_when_stored():
    assert gen._gate_facts(SimpleNamespace(approval_gates=None)) == {}
    assert gen._gate_facts(SimpleNamespace(approval_gates=[{"action": "a"}])) == {"gates": [{"action": "a"}]}


def test_the_seeded_gates_of_the_threat_response_fleet_are_stored_without_tools():
    """#1675: a seeded gate is a decision; none names a tool."""
    fleet = _seeded_fleet("ctem-loop")
    keys = {g["key"] for g in fleet.approval_gates}
    assert {"ctem.validation.non_readonly", "ctem.mobilize"} <= keys
    assert not [g["key"] for g in fleet.approval_gates if g.get("tools")]


def test_the_seed_is_idempotent():
    fleet = _seeded_fleet("ctem-loop")
    stored = fleet.approval_gates
    _seed_approval_gates(fleet, BUILTIN_FLEET_STRUCTURED["ctem-loop"])
    assert fleet.approval_gates is stored
