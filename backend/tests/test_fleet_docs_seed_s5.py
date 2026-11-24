"""#9768 S5: built-in fleet docs carry the generator's computed facts; orchestrators know the tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import fleet_seed
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.services.fleet_doc_generation import build_structured, with_computed_facts, with_derived_parts
from app.services.fleet_docs import clean_structured

_CONTEXT = {
    "automations": [
        {
            "name": "Weekly threat sweep",
            "member": "Loop Orchestrator",
            "trigger": "On a schedule",
            "schedule": "Every Monday at 07:00 (UTC)",
            "event": "",
            "enabled": False,
            "run_as": "its owner",
        }
    ],
    "schedule_quota": [
        {
            "trigger": "Weekly threat sweep",
            "basis": "Estimate: one run per tick of the schedule over 30 days",
            "runs_min": 0,
            "runs_max": 5,
        }
    ],
    "tools": [
        {
            "member": "Loop Orchestrator",
            "integrations": ["opencti"],
            "mcp_servers": [],
            "tool_groups": ["shared_state"],
            "skills": [],
        }
    ],
}


def test_the_computed_facts_go_on_top_of_the_shipped_parts():
    shipped = BUILTIN_FLEET_STRUCTURED["ctem-loop"]
    merged = with_computed_facts(shipped, _CONTEXT)
    assert merged["automations"] == _CONTEXT["automations"]
    assert merged["integrations"] == _CONTEXT["tools"]
    sweep = [q for q in merged["quota"] if q["trigger"] == "Weekly threat sweep"]
    assert sweep == _CONTEXT["schedule_quota"], "a schedule's quota comes from its cron, never the shipped estimate"
    assert merged["purpose_line"] == shipped["purpose_line"]
    assert "automations" not in shipped, "the shipped constant is never mutated"
    assert clean_structured(merged)


def test_the_generator_and_the_seed_share_one_merge():
    notes: list[str] = []
    generated = build_structured({"purpose_line": "Triage threats."}, _CONTEXT, notes)
    # The generator adds the parts derived from the configuration (#9979) on top of the shared merge.
    shared = with_computed_facts({"purpose_line": "Triage threats."}, _CONTEXT)
    assert generated == clean_structured(with_derived_parts(shared, _CONTEXT, []))
    assert generated is not None
    assert {k: v for k, v in generated.items() if k != "prerequisites"} == clean_structured(shared)


def test_no_context_leaves_the_parts_as_shipped():
    shipped = {"purpose_line": "One line."}
    assert with_computed_facts(shipped, {}) == shipped


@pytest.mark.asyncio
async def test_the_seed_merges_the_computed_facts_and_survives_a_failed_read(monkeypatch):
    from app.services import fleet_doc_generation

    fleet = SimpleNamespace(slug="ctem-loop")

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(begin_nested=_Savepoint)

    async def context(_db, _fleet):
        return _CONTEXT

    monkeypatch.setattr(fleet_doc_generation, "load_draft_context", context)
    merged = await fleet_seed._with_computed_facts(db, fleet, {"purpose_line": "One line."})  # type: ignore[arg-type]
    assert merged["automations"] and merged["integrations"]

    async def broken(_db, _fleet):
        raise RuntimeError("read failed")

    monkeypatch.setattr(fleet_doc_generation, "load_draft_context", broken)
    kept = {"purpose_line": "One line."}
    assert await fleet_seed._with_computed_facts(db, fleet, kept) is kept  # type: ignore[arg-type]
    assert await fleet_seed._with_computed_facts(None, fleet, None) is None  # type: ignore[arg-type]


_PERSONAS = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_built_in_orchestrator_is_told_to_regenerate_the_documentation(fleet):
    from app.agents.builtin_agents import BUILTIN_AGENTS

    agent = next(a for a in BUILTIN_AGENTS if a.get("slug") == fleet["orchestrator"])
    persona = agent.get("persona") or ""
    path = _PERSONAS / f"{persona}.md"
    text = path.read_text() if path.is_file() else persona
    assert "regenerate_fleet_documentation" in text, fleet["orchestrator"]
