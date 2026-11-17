"""Skill fork on PostgreSQL (#9174): an editable personal copy of any skill.

* **Built-in:** a seeded skill forks into a personal skill without the seed
  markers, its resources copied, its history restarted at v1, provenance kept.
* **Repoint:** with a fleet, the member agents the caller may edit are re-bound
  to the copy, their load mode kept; the source skill is untouched.
* **No content:** a skill never fetched is refused.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_resource import AgentSkillResource
from app.models.fleet import Fleet
from app.models.skill_version import SkillVersion
from app.models.user import User
from app.services.builtin_tools.fleets import build_fleet_tools
from app.services.fleets import FleetActionRefusedError
from app.services.skill_fork import fork_skill, fork_tags, repoint_skill_ids, seed_slug
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with (
        scratch_database("skillfork", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def _user(session, name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _builtin_skill(session) -> AgentSkill:
    skill = AgentSkill(
        user_id=None,
        is_company_managed=True,
        name="Triage",
        description="How to triage",
        content="# Triage\nRead references/steps.md",
        license="Apache-2.0",
        tags=["builtin", "cti", "slug:triage"],
        enabled=True,
    )
    session.add(skill)
    await session.flush()
    session.add(
        AgentSkillResource(
            skill_id=skill.id, path="references/steps.md", content=b"step 1", size=6, sha256="0" * 64
        )
    )
    session.add(SkillVersion(skill_id=skill.id, version_number=3, content=skill.content))
    await session.flush()
    return skill


def test_fork_tags_and_seed_slug():
    tags = ["builtin", "cti", "slug:triage"]
    assert fork_tags(tags) == ["cti"]
    assert seed_slug(tags) == "triage"
    assert seed_slug(["cti"]) is None


def test_repoint_keeps_load_mode_and_dedupes():
    agent = Agent(skill_ids=["a", "s", "f"], skill_load_modes={"s": "always"})
    assert repoint_skill_ids(agent, "s", "f") is True
    assert agent.skill_ids == ["a", "f"]
    assert agent.skill_load_modes == {"f": "always"}
    assert repoint_skill_ids(agent, "s", "f") is False


async def test_fork_builtin_skill_copies_content_resources_and_provenance():
    async with _database() as session:
        alice = await _user(session, "alice")
        source = await _builtin_skill(session)
        result = await fork_skill(session, source, alice, [])
        await session.commit()
        fork = result.skill
        assert fork.user_id == alice.id
        assert fork.is_company_managed is False
        assert fork.name == "Copy of Triage"
        assert fork.content == source.content
        assert fork.license == "Apache-2.0"
        assert fork.tags == ["cti"]
        assert fork.url is None
        assert result.resources_copied == 1
        provenance = fork.forked_from
        assert provenance["skill_id"] == str(source.id)
        assert provenance["slug"] == "triage"
        assert provenance["is_builtin"] is True
        assert provenance["version_number"] == 3
        versions = (
            (await session.execute(select(SkillVersion.version_number).where(SkillVersion.skill_id == fork.id)))
            .scalars()
            .all()
        )
        assert versions == [1]
        # The source is untouched.
        await session.refresh(source)
        assert source.tags == ["builtin", "cti", "slug:triage"]
        assert source.forked_from is None


async def test_fork_repoints_the_fleet_members_the_caller_edits():
    async with _database() as session:
        alice = await _user(session, "alice")
        source = await _builtin_skill(session)
        sid = str(source.id)
        ea = Agent(id=uuid.uuid4(), user_id=alice.id, name="EA", slug=f"ea-{uuid.uuid4().hex[:6]}", persona="p")
        dev = Agent(
            id=uuid.uuid4(),
            user_id=alice.id,
            name="Dev",
            slug=f"dev-{uuid.uuid4().hex[:6]}",
            persona="p",
            skill_ids=[sid],
            skill_load_modes={sid: "always"},
        )
        session.add_all([ea, dev])
        await session.commit()
        _defs, executor = build_fleet_tools(str(alice.id))
        answer = await executor.handler("create_fleet")(
            name="Office", orchestrator_agent_id=str(ea.id), member_agent_ids=[str(dev.id)]
        )
        fleet_id = re.search(r"ID: `([0-9a-f-]{36})`", answer).group(1)  # type: ignore[union-attr]
        fleet = await session.get(Fleet, uuid.UUID(fleet_id))
        result = await fork_skill(session, source, alice, [], name="My triage", repoint_fleet=fleet)
        await session.commit()
        await session.refresh(dev)
        fid = str(result.skill.id)
        assert result.skill.name == "My triage"
        assert result.repointed_agent_ids == [dev.id]
        assert dev.skill_ids == [fid]
        assert dev.skill_load_modes == {fid: "always"}
        assert result.warnings == []


async def test_fork_refuses_a_skill_without_content():
    async with _database() as session:
        alice = await _user(session, "alice")
        skill = AgentSkill(user_id=alice.id, name="Remote", url="https://example.com/SKILL.md", content=None)
        session.add(skill)
        await session.flush()
        with pytest.raises(FleetActionRefusedError) as refused:
            await fork_skill(session, skill, alice, [])
        assert refused.value.status_code == 400
