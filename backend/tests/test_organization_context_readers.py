"""The fleets that read each part of the organization profile (#9173, S2).

Read from the fleets' documented prerequisites, never from a hand-written list:
the profile checks known to the About page are the ones the Organization page
maps, and every built-in CTEM fleet that needs a profile field is named on it.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.builtin_profile_readers import BUILTIN_AGENT_PROFILE_FIELDS, PROFILE_FIELD_WORDS
from app.services import fleet_about
from app.services.fleet_docs import PREREQUISITE_CHECKS
from app.services.organization_context import OrganizationProfile
from app.services.organization_context_readers import (
    PROFILE_CHECK_FIELDS,
    readers_from_agents,
    readers_from_fleets,
)

_PERSONAS = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"


def _persona(slug: str) -> str:
    agent = next((a for a in BUILTIN_AGENTS if a.get("slug") == slug), None)
    assert agent is not None, f"{slug} is not a seeded agent"
    return (_PERSONAS / f"{agent.get('persona', slug)}.md").read_text(encoding="utf-8").lower()


def _fleet(name: str, structured: object) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name, documentation={"sections": {}, "structured": structured})


def test_every_profile_field_check_of_the_documentation_is_mapped_to_a_real_profile_field():
    profile_checks = {
        c for c in PREREQUISITE_CHECKS if c.startswith("profile:") and c != "profile:tracked_organizations"
    }
    assert profile_checks == set(PROFILE_CHECK_FIELDS)
    fields = set(OrganizationProfile.model_fields)
    assert set(PROFILE_CHECK_FIELDS.values()) <= fields
    # The About page and the Organization page read one mapping.
    assert fleet_about._PROFILE_FIELDS is PROFILE_CHECK_FIELDS


def test_a_fleet_is_named_once_on_each_field_it_needs():
    response = _fleet(
        "Threat response",
        {
            "prerequisites": [
                {"check": "integration:opencti"},
                {"check": "profile:sector"},
                {"check": "profile:crown_jewels"},
                {"check": "profile:crown_jewels"},
                {"check": "manual"},
            ]
        },
    )
    readers = readers_from_fleets([response])
    assert set(readers) == {"sector", "crown_jewels"}
    assert readers["crown_jewels"] == [{"fleet_id": str(response.id), "name": "Threat response"}]


def test_a_fleet_without_structured_documentation_reads_nothing():
    fleets = [
        SimpleNamespace(id=uuid.uuid4(), name="Plain", documentation=None),
        SimpleNamespace(id=uuid.uuid4(), name="Sections only", documentation={"sections": {"purpose": "x"}}),
        _fleet("Odd", {"prerequisites": "not a list"}),
        _fleet("Odd rows", {"prerequisites": ["profile:sector", None]}),
    ]
    assert readers_from_fleets(fleets) == {}


def test_the_built_in_ctem_fleets_name_the_fields_they_need():
    fleets = [_fleet(slug, structured) for slug, structured in BUILTIN_FLEET_STRUCTURED.items()]
    readers = readers_from_fleets(fleets)
    named = {field: [row["name"] for row in rows] for field, rows in readers.items()}
    assert "ctem-loop" in named["crown_jewels"]
    assert "ctem-vuln-prioritization" in named["tech_stack"]
    assert named["assets_exposure"] == ["ctem-ai-security"]
    # The retired fleets (CTEM-31) read nothing: they are gone from the catalog.
    assert all("ctem-posture" not in names for names in named.values())


def test_every_seeded_persona_that_reads_the_profile_is_in_the_agent_table():
    reading = set()
    for agent in BUILTIN_AGENTS:
        slug = agent.get("slug")
        path = _PERSONAS / f"{agent.get('persona', slug)}.md"
        if path.is_file() and "get_organization_context" in path.read_text(encoding="utf-8"):
            reading.add(slug)
    assert reading == set(BUILTIN_AGENT_PROFILE_FIELDS)


def test_each_agent_entry_names_only_real_fields_its_persona_mentions():
    fields = set(OrganizationProfile.model_fields)
    assert set(PROFILE_FIELD_WORDS) <= fields
    for slug, read in BUILTIN_AGENT_PROFILE_FIELDS.items():
        persona = _persona(slug)
        for field in read:
            assert field in PROFILE_FIELD_WORDS, f"{slug}: {field} has no persona word"
            assert PROFILE_FIELD_WORDS[field] in persona, f"{slug} lists {field} its persona never mentions"


def test_the_pirs_and_the_risk_appetite_have_agent_readers():
    readers = {field for read in BUILTIN_AGENT_PROFILE_FIELDS.values() for field in read}
    assert {"pir_ids", "risk_appetite", "currency", "ticketing"} <= readers


def test_only_seeded_agents_of_the_table_are_named_once_per_field():
    quantifier = SimpleNamespace(id=uuid.uuid4(), slug="ctem-risk-quantifier", name="Risk Quantifier", is_builtin=True)
    fork = SimpleNamespace(id=uuid.uuid4(), slug="ctem-risk-quantifier-copy", name="Copy", is_builtin=False)
    custom = SimpleNamespace(id=uuid.uuid4(), slug="ctem-risk-quantifier", name="Mine", is_builtin=False)
    readers = readers_from_agents([quantifier, fork, custom])
    assert set(readers) == {"crown_jewels", "risk_appetite", "currency", "fx_rates"}
    assert readers["risk_appetite"] == [{"agent_id": str(quantifier.id), "name": "Risk Quantifier"}]
