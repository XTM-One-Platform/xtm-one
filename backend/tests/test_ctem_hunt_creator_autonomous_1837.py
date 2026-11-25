"""The Threat Hunt Creator has an autonomous path (#1837, gap ctem-loop-hunt-creator-no-autonomous-path).

The CTEM loop consults it from the Threat Relevance Analyst and the PIR desk
orchestrator delegates to it: no person answers a question or approves a plan in
those runs. Its persona must say how it runs then (inputs, platform choice,
draft, output) and never stop the run for a person: a choice a person should
make goes back to the caller, which files the decision between its steps
(Sam, 24 Nov 2026: no tool gate, no approval card inside a seeded fleet run).
"""

from __future__ import annotations

from pathlib import Path

from app.agents.builtin_agents import BUILTIN_AGENTS

_PERSONA = Path(__file__).resolve().parents[1] / "app/agents/seed_data/agent_persona/opencti-threat-hunt-creator.md"


def _text() -> str:
    return _PERSONA.read_text(encoding="utf-8")


def test_the_persona_has_an_autonomous_path_section():
    text = _text()
    assert "## Autonomous path" in text
    section = text.split("## Autonomous path", 1)[1].split("\n## ", 1)[0]
    for needle in ("**Inputs**", "**Platform.**", "get_organization_context", "**Draft.**", "**Output.**", "`missing`"):
        assert needle in section, needle


def test_the_autonomous_path_never_waits_for_a_person():
    text = _text()
    assert "never ask one and never wait for an approval" in text
    assert "you never stop the run for it" in text
    # The person path keeps its plan approval, scoped to the person.
    assert "With a person, always present the hunt plan" in text


def test_the_autonomous_path_creates_its_own_draft():
    assert "On the autonomous path, always create a new draft" in _text()


def test_the_hunt_creator_keeps_the_tools_the_autonomous_path_names():
    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "opencti-threat-hunt-creator")
    assert "opencti" in agent["integration_template_bindings"]
    assert "opencti-detection-rule-converter" in agent["handover_agent_slugs"]
