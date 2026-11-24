"""Seeded CTEM agents carry guardrails (#1837, gap ctem-seed-agent-hygiene).

The guardrails are facts the agent decides with: no gate, approval card or refusal
is added inside a run (Sam, 24 Nov 2026). The seed writes them at every boot.
"""

from app.agents.builtin_agents import BUILTIN_AGENTS, CTEM_SEED_GUARDRAILS
from app.agents.seed import _sync_agent_fields
from app.models.agent import Agent


def _seeded(data: dict) -> Agent:
    agent = Agent(slug=data["slug"], name=data["name"])
    _sync_agent_fields(agent, data)
    return agent


def test_every_ctem_seed_is_stored_with_guardrails() -> None:
    missing = sorted(
        data["slug"]
        for data in BUILTIN_AGENTS
        if "ctem" in (data.get("tags") or []) and not (_seeded(data).guardrails or "").strip()
    )
    assert missing == []


def test_the_guardrails_state_facts_and_add_no_gate() -> None:
    text = CTEM_SEED_GUARDRAILS.lower()
    for word in ("approval", "approve", "refuse", "ask the user", "confirm before"):
        assert word not in text


def test_a_definition_without_guardrails_leaves_the_field_empty() -> None:
    agent = Agent(slug="x", name="X", guardrails="stale")
    _sync_agent_fields(agent, {"slug": "x", "name": "X"})
    assert agent.guardrails is None
