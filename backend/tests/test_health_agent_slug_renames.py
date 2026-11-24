"""The Platform Health fleet's agent slugs (#101 S1): the former Concierge slugs renamed.

* The seed specs carry no ``concierge`` slug, handover, assignment slug or tag;
  every former slug maps to one current slug and its persona file follows.
* Against PostgreSQL (``integration``-marked, skipped when unreachable): a fleet
  seeded under the former slugs is adopted in place by the next seed (same
  ids, handovers and assignments kept, nothing created twice, tag retagged); a
  row already on the current slug wins over a former one, which is left as is;
  a personal agent on a former slug is never renamed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents.seed import SEED_DATA_DIR
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services.health import fleet_seed

_FORMER = {
    "concierge": "platform-health",
    "concierge-memory-steward": "health-memory-steward",
    "concierge-quality-analyst": "health-quality-analyst",
    "concierge-flow-lab": "health-flow-lab",
    "concierge-experimenter": "health-experimenter",
}


def test_no_seed_spec_carries_a_concierge_slug() -> None:
    slugs = [spec.slug for spec in fleet_seed.FLEET_AGENTS]
    handovers = [slug for spec in fleet_seed.FLEET_AGENTS for slug, _ in spec.handovers]
    assignments = [spec.agent_slug for spec in fleet_seed.FLEET_ASSIGNMENTS]
    assert [slug for slug in (*slugs, *handovers, *assignments) if "concierge" in slug] == []
    assert fleet_seed.ORCHESTRATOR_SLUG == "platform-health"
    assert fleet_seed.FLEET_AGENT_TAG == "health"


def test_every_former_slug_maps_to_one_current_slug() -> None:
    # S15a-1 (#101): the S14 Module Analyst is renamed in place to the OpenCTI Health Checker.
    assert fleet_seed.RENAMED_FLEET_AGENT_SLUGS == {**_FORMER, "health-module-analyst": "health-opencti-checker"}
    current = {spec.slug for spec in fleet_seed.FLEET_AGENTS}
    assert set(fleet_seed.RENAMED_FLEET_AGENT_SLUGS) & current == set()


def test_the_persona_files_follow_the_slugs() -> None:
    personas = SEED_DATA_DIR / "agent_persona"
    for spec in fleet_seed.FLEET_AGENTS:
        assert (personas / f"{spec.slug}.md").is_file(), spec.slug
        for former in spec.former_slugs:
            assert not (personas / f"{former}.md").exists(), former


def _row(slug: str, *, company_managed: bool = True, tags: list[str] | None = None) -> Agent:
    return Agent(slug=slug, name=slug, is_company_managed=company_managed, tags=tags or ["concierge"])


def test_a_row_on_a_former_slug_is_adopted_and_retagged() -> None:
    row = _row("concierge-flow-lab", tags=["concierge", "kept"])
    by_slug = fleet_seed._adopt_former_slugs([row])
    assert by_slug == {"health-flow-lab": row}
    assert (row.slug, row.tags) == ("health-flow-lab", ["kept", "health"])


def test_a_row_on_the_current_slug_wins_over_a_former_one() -> None:
    current, former = _row("health-experimenter", tags=["health"]), _row("concierge-experimenter")
    by_slug = fleet_seed._adopt_former_slugs([former, current])
    assert by_slug == {"health-experimenter": current}
    assert former.slug == "concierge-experimenter"


def test_a_personal_agent_on_a_former_slug_is_never_renamed() -> None:
    personal = _row("concierge", company_managed=False)
    assert fleet_seed._adopt_former_slugs([personal]) == {}
    assert personal.slug == "concierge"


@pytest.mark.integration
async def test_a_fleet_seeded_under_the_former_slugs_is_adopted_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.health_support import World, health_database

    async with health_database() as sessions:
        await World().build(sessions)
        async with sessions() as db:
            await fleet_seed.seed_health_fleet(db)
        async with sessions() as db:
            agents = (await db.execute(select(Agent).where(Agent.slug.in_(list(_FORMER.values()))))).scalars().all()
            before = {agent.slug: (agent.id, list(agent.handover_agent_ids or [])) for agent in agents}
            assignments = {row.id for row in (await db.execute(select(Assignment))).scalars()}
            # A deployment seeded before #101 S1 carries the former Concierge slugs and tag.
            current_to_former = {current: former for former, current in _FORMER.items()}
            for agent in agents:
                agent.slug = current_to_former[agent.slug]
                agent.tags = ["concierge"]
            await db.commit()
        async with sessions() as db:
            created = await fleet_seed.seed_health_fleet(db)
        async with sessions() as db:
            agents = (await db.execute(select(Agent))).scalars().all()
            after = {agent.slug: (agent.id, list(agent.handover_agent_ids or [])) for agent in agents}
            tags = {agent.slug: agent.tags for agent in agents}
            reseeded = {row.id for row in (await db.execute(select(Assignment))).scalars()}
    assert created == 0
    assert set(before) == set(_FORMER.values())
    assert {slug: after[slug] for slug in before} == before
    assert [slug for slug in after if "concierge" in slug] == []
    assert all(tags[slug] == ["health"] for slug in before)
    assert reseeded == assignments
