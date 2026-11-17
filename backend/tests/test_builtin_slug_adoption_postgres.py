"""The seed never turns a person's agent into a built-in.

A built-in agent is keyed by its slug. A person can create an agent today under
the slug a later release gives a new built-in; on that upgrade the seed used to
adopt it - flip ``is_builtin`` and overwrite its persona and tools - leaving the
person owning a "built-in" that is neither company-managed nor theirs any more,
and the real built-in never created. The seed now moves such an agent aside to
a free slug and creates the built-in - an agent an earlier boot already
adopted included - and the stale sweep of retired built-in slugs hands such an
agent back instead of deleting it. An agent that belongs to the company
(company-managed, the shape every seeded row has) is still adopted.

An agent an earlier boot adopted also had the seed's rows attached to it by
slug: assignments, global intent bindings, built-in knowledge base, skill and
integration bindings, the built-in's tags and intents. Handing it back takes
those away and leaves what the person added.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper
from app.agents import seed
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.models.intent import Intent, IntentBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_SLUG = "future-builtin"
_ENTRY = {
    "slug": _SLUG,
    "persona": "You are a future built-in.",
    "name": "Future Built-in",
    "description": "Shipped by a later release.",
    "icon": "Bot",
    "output_format": "markdown",
    "disable_chat": False,
    "max_iterations": 3,
    "tags": ["builtin"],
    "handover_agent_slugs": [],
    "handover_type_slugs": {},
    "builtin_tool_ids": [],
    "integration_template_bindings": [],
}


async def _person(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"person-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Person",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed(monkeypatch, sessions) -> None:
    monkeypatch.setattr(seed, "_seeded_builtin_agents", lambda: [dict(_ENTRY)])
    async with sessions() as session:
        await seed.seed_builtin_agents(session, platform_mode="xtm_one")
        await session.commit()


@pytest.mark.asyncio
async def test_a_persons_agent_on_a_builtin_slug_is_moved_aside(monkeypatch):
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            person = await _person(session)
            mine = Agent(
                id=uuid.uuid4(),
                user_id=person.id,
                is_company_managed=False,
                slug=_SLUG,
                name="My agent",
                persona="My own persona.",
            )
            session.add(mine)
            await session.commit()
            mine_id, person_id = mine.id, person.id

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            kept = await session.get(Agent, mine_id)
            builtin = (
                await session.execute(select(Agent).where(Agent.slug == _SLUG))
            ).scalar_one()

    assert kept.slug == f"{_SLUG}-2"
    assert kept.is_builtin is False
    assert kept.user_id == person_id
    assert kept.is_company_managed is False
    assert kept.persona == "My own persona."
    assert builtin.id != mine_id
    assert builtin.is_builtin is True
    assert builtin.is_company_managed is True
    assert builtin.persona == "You are a future built-in."


@pytest.mark.asyncio
async def test_a_company_managed_agent_on_a_builtin_slug_is_still_adopted(monkeypatch):
    """The shape every seeded row has - a legacy one included - is the company's."""
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            legacy = Agent(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                is_builtin=False,
                slug=_SLUG,
                name="Legacy copy",
                persona="Old persona.",
            )
            session.add(legacy)
            await session.commit()
            legacy_id = legacy.id

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            rows = (
                (await session.execute(select(Agent).where(Agent.slug.like(f"{_SLUG}%"))))
                .scalars()
                .all()
            )

    assert [r.id for r in rows] == [legacy_id]
    assert rows[0].is_builtin is True
    assert rows[0].persona == "You are a future built-in."


@pytest.mark.asyncio
async def test_a_persons_agent_an_earlier_boot_adopted_is_handed_back(monkeypatch):
    """The shape the bug left behind - ``is_builtin`` set, still private to its
    owner - is moved aside too, as an ordinary agent, and the built-in created."""
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            person = await _person(session)
            adopted = Agent(
                id=uuid.uuid4(),
                user_id=person.id,
                is_company_managed=False,
                is_builtin=True,
                slug=_SLUG,
                name="My agent",
                persona="Overwritten persona.",
            )
            session.add(adopted)
            await session.commit()
            adopted_id, person_id = adopted.id, person.id

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            kept = await session.get(Agent, adopted_id)
            builtin = (
                await session.execute(select(Agent).where(Agent.slug == _SLUG))
            ).scalar_one()

    assert kept.slug == f"{_SLUG}-2"
    assert kept.is_builtin is False
    assert kept.user_id == person_id
    assert kept.is_company_managed is False
    assert builtin.id != adopted_id
    assert builtin.is_builtin is True
    assert builtin.is_company_managed is True


@pytest.mark.asyncio
async def test_a_retired_builtin_slug_releases_a_persons_agent_instead_of_deleting_it(
    monkeypatch,
):
    """The stale sweep deletes retired built-ins - the company's, never a
    person's agent an earlier boot adopted."""
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            person = await _person(session)
            adopted = Agent(
                id=uuid.uuid4(),
                user_id=person.id,
                is_company_managed=False,
                is_builtin=True,
                slug="retired-builtin",
                name="My agent",
            )
            retired = Agent(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                is_builtin=True,
                slug="another-retired-builtin",
                name="Retired built-in",
            )
            session.add_all([adopted, retired])
            await session.commit()
            adopted_id, retired_id = adopted.id, retired.id

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            kept = await session.get(Agent, adopted_id)
            gone = await session.get(Agent, retired_id)

    assert kept is not None
    assert kept.is_builtin is False
    assert kept.slug == "retired-builtin"
    assert gone is None


def _seed_owns(monkeypatch) -> None:
    """What the seed attaches to ``future-builtin`` besides the agent itself."""
    monkeypatch.setattr(seed, "BUILTIN_AGENTS", [{**_ENTRY, "tags": ["builtin", "future"]}])
    monkeypatch.setattr(
        seed,
        "BUILTIN_ASSIGNMENTS",
        [{"agent_slug": _SLUG, "name": "Seeded briefing"}],
    )
    monkeypatch.setattr(
        seed,
        "BUILTIN_KNOWLEDGE_BASES",
        [{"slug": "seeded-kb", "bind_to_agent_slugs": [_SLUG]}],
    )
    monkeypatch.setattr(
        seed, "BUILTIN_SKILLS", [{"slug": "seeded-skill", "bind_to_agent_slugs": [_SLUG]}]
    )
    monkeypatch.setattr(
        seed,
        "BUILTIN_INTEGRATIONS",
        [{"provider": "opencti", "name": "Seeded", "bind_to_agent_slugs": [_SLUG]}],
    )


async def _adopted_with_seed_rows(session, person: User, slug: str) -> dict:
    """A person's agent as an adopting boot left it, with the seed's rows on it
    and one row of each kind the person added themselves."""
    seeded_kb = KnowledgeBase(
        is_builtin=True, is_company_managed=True, name="Seeded KB",
        tags=["builtin", "slug:seeded-kb"],
    )
    own_kb = KnowledgeBase(user_id=person.id, name="My KB", tags=[])
    seeded_skill = AgentSkill(
        is_company_managed=True, name="Seeded skill", tags=["builtin", "slug:seeded-skill"],
    )
    own_skill = AgentSkill(user_id=person.id, name="My skill", tags=[])
    seeded_int = Integration(
        provider="opencti", name="Seeded", is_builtin=True, is_company_managed=True,
    )
    own_int = Integration(provider="opencti", name="Mine", user_id=person.id)
    session.add_all([seeded_kb, own_kb, seeded_skill, own_skill, seeded_int, own_int])
    await session.flush()

    adopted = Agent(
        id=uuid.uuid4(),
        user_id=person.id,
        is_company_managed=False,
        is_builtin=True,
        slug=slug,
        name="My agent",
        tags=["builtin", "future", "mine"],
        supported_intents={"global.assistant": 1},
        knowledge_base_ids=[str(seeded_kb.id), str(own_kb.id)],
        skill_ids=[str(seeded_skill.id), str(own_skill.id)],
        integration_bindings={"opencti": [str(seeded_int.id), str(own_int.id)]},
    )
    session.add(adopted)
    await session.flush()

    seeded_asg = Assignment(
        agent_id=adopted.id, user_id=person.id, name="Seeded briefing",
        trigger_type="manual", trigger_config={}, prompt="Seeded.",
    )
    own_asg = Assignment(
        agent_id=adopted.id, user_id=person.id, name="My assignment",
        trigger_type="manual", trigger_config={}, prompt="Mine.",
    )
    intent = Intent(name=f"test.intent.{uuid.uuid4().hex[:8]}")
    session.add_all([seeded_asg, own_asg, intent])
    await session.flush()
    global_binding = IntentBinding(
        intent_id=intent.id, agent_id=adopted.id, user_id=None, is_builtin=True,
    )
    own_binding = IntentBinding(
        intent_id=intent.id, agent_id=adopted.id, user_id=person.id, is_builtin=False,
    )
    session.add_all([global_binding, own_binding])
    await session.commit()
    return {
        "agent": adopted.id,
        "own_kb": str(own_kb.id),
        "own_skill": str(own_skill.id),
        "own_int": str(own_int.id),
        "own_asg": own_asg.id,
        "own_binding": own_binding.id,
    }


async def _left_on(session, agent_id) -> tuple[Agent, list, list]:
    agent = await session.get(Agent, agent_id)
    assignments = (
        await session.execute(select(Assignment.id).where(Assignment.agent_id == agent_id))
    ).scalars().all()
    bindings = (
        await session.execute(
            select(IntentBinding.id).where(IntentBinding.agent_id == agent_id)
        )
    ).scalars().all()
    return agent, list(assignments), list(bindings)


@pytest.mark.asyncio
async def test_a_moved_agent_keeps_none_of_the_rows_the_seed_attached(monkeypatch):
    _seed_owns(monkeypatch)
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            person = await _person(session)
            ids = await _adopted_with_seed_rows(session, person, _SLUG)

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            kept, assignments, bindings = await _left_on(session, ids["agent"])

    assert kept.slug == f"{_SLUG}-2"
    assert kept.is_builtin is False
    assert assignments == [ids["own_asg"]]
    assert bindings == [ids["own_binding"]]
    assert kept.knowledge_base_ids == [ids["own_kb"]]
    assert kept.skill_ids == [ids["own_skill"]]
    assert kept.integration_bindings == {"opencti": [ids["own_int"]]}
    assert kept.tags == ["mine"]
    assert kept.supported_intents == {}


@pytest.mark.asyncio
async def test_a_released_agent_keeps_none_of_the_global_intent_bindings(monkeypatch):
    """The retired-slug sweep hands the agent back the same way."""
    _seed_owns(monkeypatch)
    async with scratch_database("slugadopt", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            person = await _person(session)
            ids = await _adopted_with_seed_rows(session, person, "retired-builtin")

        await _seed(monkeypatch, db.sessions)

        async with db.sessions() as session:
            kept, assignments, bindings = await _left_on(session, ids["agent"])

    assert kept.slug == "retired-builtin"
    assert kept.is_builtin is False
    assert bindings == [ids["own_binding"]]
    assert kept.tags == ["future", "mine"]
    assert kept.supported_intents == {}
    # Nothing else names the retired slug any more, so its seeded
    # assignments and bindings cannot be told from the person's: both stay.
    assert len(assignments) == 2
    assert len(kept.knowledge_base_ids) == 2
