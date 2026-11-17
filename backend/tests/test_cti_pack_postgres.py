"""Template default skills, manual inputs and the CTI pack seed, against a real PostgreSQL.

An agent template names default skills by built-in slug and its default
assignments may declare ``manual_trigger_parameters``. Driven through the real
routers and the agent-builder tool as an ordinary (non-admin) user:

* the template list resolves the defaults to the skills the caller may attach
  (enabled, company-managed, visible: an ``access_restricted`` skill only to an
  admin) - what the wizard preselects;
* a create that omits ``skill_ids`` gets them, an explicit list (even empty) is
  kept as sent, and the builder ``create_agent`` (which takes no skill list)
  gets them too;
* both create paths copy the declared inputs onto the assignments, a malformed
  entry dropped;
* the XTM One seed ships the CTI templates (with their default skills) and the
  CTI skills, the bundled resources of a skill synced to its seed files.

Same scratch-database recipe as ``test_assignment_text_length_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.agents.cti_skills import CTI_SKILLS
from app.agents.cti_templates import CTI_TEMPLATES
from app.agents.seed import (
    SEED_DATA_DIR,
    SKILL_RESOURCES_DIR,
    seed_builtin_skills,
    seed_builtin_templates,
)
from app.api import deps
from app.api.v1 import agent_skills as skills_api
from app.api.v1 import agent_templates as templates_api
from app.api.v1 import agents as agents_api
from app.api.v1 import public_catalog as catalog_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_resource import AgentSkillResource
from app.models.agent_template import AgentTemplate
from app.models.assignment import Assignment
from app.models.user import User
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_LICENSE_BYTES = (SEED_DATA_DIR / "licenses" / "cti-skills-LICENSE.md").read_bytes()

_INPUT = {
    "key": "source_material",
    "label": "Source material",
    "type": "textarea",
    "required": False,
}


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as db:
        yield db.sessions


async def _user(sessions: async_sessionmaker, *, is_admin: bool = False) -> User:
    async with sessions() as db:
        user = User(
            email=f"cti-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="CTI Test",
            role="admin" if is_admin else "user",
        )
        db.add(user)
        await db.commit()
    return user


async def _skill(sessions, slug: str, **fields) -> str:
    async with sessions() as db:
        skill = AgentSkill(
            name=f"Skill {slug}",
            content="# body",
            is_company_managed=True,
            tags=["builtin", f"slug:{slug}"],
            **fields,
        )
        db.add(skill)
        await db.commit()
        return str(skill.id)


async def _fixture(sessions) -> tuple[str, dict[str, str]]:
    """A template naming four default skills, one per resolution outcome."""
    ids = {
        "visible": await _skill(sessions, "t-visible"),
        "disabled": await _skill(sessions, "t-disabled", enabled=False),
        "restricted": await _skill(sessions, "t-restricted", access_restricted=True),
    }
    slug = f"cti-test-{uuid.uuid4().hex[:8]}"
    async with sessions() as db:
        db.add(
            AgentTemplate(
                slug=slug,
                name="CTI test template",
                default_skills=["t-restricted", "t-visible", "t-disabled", "t-missing"],
                default_assignments=[
                    {
                        "name": "Analyse",
                        "trigger_type": "manual",
                        "prompt": "Read `source_material`.",
                        "manual_trigger_parameters": [
                            _INPUT,
                            {"label": "no key"},
                            "not an object",
                            {"key": "  ", "label": "blank key"},
                        ],
                    },
                    {"name": "No inputs", "trigger_type": "manual", "prompt": "Go."},
                ],
            )
        )
        await db.commit()
    return slug, ids


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(templates_api.template_router, prefix="/api/v1/agents")
    app.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _created_agent(sessions) -> tuple[Agent, list[Assignment]]:
    async with sessions() as db:
        [agent] = (await db.execute(select(Agent))).scalars().all()
        assignments = (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent.id).order_by(Assignment.name)
            )
        ).scalars().all()
        return agent, list(assignments)


# ---------------------------------------------------------------------------
# Template list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_admin", "expected"), [(False, ["visible"]), (True, ["restricted", "visible"])])
async def test_the_template_list_resolves_default_skills_for_the_caller(is_admin, expected):
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions, is_admin=is_admin)
        slug, ids = await _fixture(sessions)
        async with _client(sessions, user) as client:
            response = await client.get("/api/v1/agents/templates")
        assert response.status_code == 200, response.text
        [row] = [t for t in response.json() if t["slug"] == slug]
        assert row["default_skills"] == ["t-restricted", "t-visible", "t-disabled", "t-missing"]
        # In template order; disabled and missing slugs never resolve.
        assert row["default_skill_ids"] == [ids[k] for k in expected]


@pytest.mark.asyncio
async def test_an_admin_saves_default_skills_and_a_bad_slug_is_a_422():
    async with _temp_database("cti_pack") as sessions:
        admin = await _user(sessions, is_admin=True)
        slug, ids = await _fixture(sessions)
        async with _client(sessions, admin) as client:
            ok = await client.put(
                f"/api/v1/agents/templates/{slug}",
                json={"default_skills": ["t-visible", "t-visible"]},
            )
            bad = await client.put(
                f"/api/v1/agents/templates/{slug}", json={"default_skills": ["Not A Slug"]}
            )
        assert ok.status_code == 200, ok.text
        assert ok.json()["default_skills"] == ["t-visible"]
        assert ok.json()["default_skill_ids"] == [ids["visible"]]
        assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Create from a template
# ---------------------------------------------------------------------------


async def _create(sessions, user: User, slug: str, via: str, **body) -> None:
    if via == "rest":
        async with _client(sessions, user) as client:
            response = await client.post(
                "/api/v1/agents",
                json={"name": "From template", "template_slug": slug, **body},
            )
        assert response.status_code == 201, response.text
        return
    _, executor = build_agent_builder_tool(None, str(user.id))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_agent",
            arguments={"name": "From template", "template_slug": slug},
        )
    )
    assert "created successfully" in result.content, result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["rest", "builder"])
async def test_an_agent_from_a_template_starts_with_its_attachable_default_skills(via):
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        slug, ids = await _fixture(sessions)

        await _create(sessions, user, slug, via)

        agent, _ = await _created_agent(sessions)
        assert agent.skill_ids == [ids["visible"]]


@pytest.mark.asyncio
async def test_a_plain_builder_create_suggests_the_craft_skills_of_its_tools_without_attaching_them() -> None:
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        report = await _skill(sessions, "report-design")
        await _skill(sessions, "video-production", enabled=False)
        _, executor = build_agent_builder_tool(cast(Any, None), str(user.id))
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="create_agent",
                arguments={
                    "name": "Plain",
                    "builtin_tool_ids": "generate_report,video_studio",
                },
            )
        )
        assert "created successfully" in result.content, result.content
        assert "Suggested skills" in result.content, result.content
        assert report in result.content
        assert "video-production" not in result.content

        agent, _ = await _created_agent(sessions)
        assert not agent.skill_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "kept"), [([], []), (["visible"], ["visible"])])
async def test_an_explicit_skill_list_is_kept_as_sent(sent, kept):
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        slug, ids = await _fixture(sessions)

        await _create(sessions, user, slug, "rest", skill_ids=[ids[k] for k in sent])

        agent, _ = await _created_agent(sessions)
        assert agent.skill_ids == [ids[k] for k in kept]


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["rest", "builder"])
async def test_template_assignments_carry_their_declared_inputs(via):
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        slug, _ids = await _fixture(sessions)

        await _create(sessions, user, slug, via)

        _, assignments = await _created_agent(sessions)
        assert [(a.name, a.manual_trigger_parameters) for a in assignments] == [
            ("Analyse", [_INPUT]),
            ("No inputs", None),
        ]


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_xtm_one_seed_ships_the_cti_pack_and_copilot_removes_it():
    async with _temp_database("cti_pack") as sessions:
        async with sessions() as db:
            await seed_builtin_templates(db, platform_mode="xtm_one")
            await seed_builtin_skills(db, platform_mode="xtm_one")
        async with sessions() as db:
            templates = {
                t.slug: t for t in (await db.execute(select(AgentTemplate))).scalars().all()
            }
            assert sorted(templates) == sorted(t["slug"] for t in CTI_TEMPLATES)
            for tmpl in CTI_TEMPLATES:
                row = templates[tmpl["slug"]]
                assert row.default_skills == tmpl["default_skills"]
                assert len(row.default_assignments) == len(tmpl["default_assignments"])

            # Every default skill resolves to a seeded skill for an ordinary user.
            user = User(email="reader@example.com", hashed_password="x", full_name="R")
            db.add(user)
            await db.commit()
        async with _client(sessions, user) as client:
            listed = (await client.get("/api/v1/agents/templates")).json()
        for row in listed:
            assert len(row["default_skill_ids"]) == len(row["default_skills"]), row["slug"]

        async with sessions() as db:
            await seed_builtin_templates(db, platform_mode="copilot")
        async with sessions() as db:
            remaining = (await db.execute(select(AgentTemplate.slug))).scalars().all()
            assert not set(remaining) & {t["slug"] for t in CTI_TEMPLATES}


async def _darkweb_resources(sessions) -> dict[str, tuple[uuid.UUID, bytes]]:
    async with sessions() as db:
        skill = next(
            s
            for s in (await db.execute(select(AgentSkill))).scalars().all()
            if "slug:cti-darkweb-collection" in (s.tags or [])
        )
        rows = (
            await db.execute(
                select(AgentSkillResource).where(AgentSkillResource.skill_id == skill.id)
            )
        ).scalars().all()
        return {r.path: (r.id, r.content) for r in rows}


@pytest.mark.asyncio
async def test_a_seeded_skill_resources_are_its_seed_files_and_resync():
    root = SKILL_RESOURCES_DIR / "cti-darkweb-collection"
    expected = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    # Its own references, plus the pack licence every CTI skill carries.
    expected["LICENSE.md"] = _LICENSE_BYTES
    async with _temp_database("cti_pack") as sessions:
        async with sessions() as db:
            await seed_builtin_skills(db, platform_mode="xtm_one")
        first = await _darkweb_resources(sessions)
        assert {p: c for p, (_i, c) in first.items()} == expected

        # Drift: one file edited in the database, one stray file added.
        edited = "references/opsec.md"
        async with sessions() as db:
            row = await db.get(AgentSkillResource, first[edited][0])
            row.content = b"tampered"
            row.sha256 = "0" * 64
            db.add(
                AgentSkillResource(
                    skill_id=row.skill_id, path="references/stray.md", content=b"x", size=1, sha256="1" * 64
                )
            )
            await db.commit()

        async with sessions() as db:
            await seed_builtin_skills(db, platform_mode="xtm_one")
        second = await _darkweb_resources(sessions)
        assert {p: c for p, (_i, c) in second.items()} == expected
        # Unchanged files keep their row: a steady-state boot rewrites nothing.
        assert {p: i for p, (i, _c) in second.items()} == {p: i for p, (i, _c) in first.items()}


@pytest.mark.asyncio
async def test_every_seeded_cti_skill_carries_the_pack_licence():
    """MIT: the licence text travels with every copy of the skill."""
    async with _temp_database("cti_pack") as sessions:
        async with sessions() as db:
            await seed_builtin_skills(db, platform_mode="xtm_one")
        async with sessions() as db:
            rows = (await db.execute(select(AgentSkill))).scalars().all()
            skills = {
                tag[len("slug:"):]: s.id
                for s in rows
                for tag in (s.tags or [])
                if isinstance(tag, str) and tag.startswith("slug:cti-")
            }
            notices = {s.id: s.license for s in rows}
            licences = {
                row.skill_id: row.content
                for row in (
                    await db.execute(
                        select(AgentSkillResource).where(AgentSkillResource.path == "LICENSE.md")
                    )
                ).scalars().all()
            }
        assert sorted(skills) == sorted(s["slug"] for s in CTI_SKILLS)
        assert {slug: licences.get(sid) for slug, sid in skills.items()} == {
            slug: _LICENSE_BYTES for slug in skills
        }
        assert {notices[sid] for sid in skills.values()} == {
            next(s["license"] for s in CTI_SKILLS)
        }


@pytest.mark.asyncio
async def test_a_catalog_clone_of_a_skill_keeps_its_resources():
    """A skill installed from the public catalog keeps its bundled files - its
    references and the licence its body points to."""
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        async with sessions() as db:
            await seed_builtin_skills(db, platform_mode="xtm_one")
            source = next(
                s
                for s in (await db.execute(select(AgentSkill))).scalars().all()
                if "slug:cti-darkweb-collection" in (s.tags or [])
            )
            source.is_public_catalog = True
            await db.commit()
            source_id = source.id
        before = await _darkweb_resources(sessions)

        app = FastAPI()
        app.include_router(catalog_api.router, prefix="/api/v1/public")

        async def _get_db():
            async with sessions() as db:
                yield db
                await db.commit()

        app.dependency_overrides[deps.get_current_user] = lambda: user
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[deps.require_ai_enabled] = lambda: None
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/api/v1/public/catalog/skills/{source_id}/add")
        assert response.status_code == 200, response.text
        clone_id = uuid.UUID(response.json()["id"])

        async with sessions() as db:
            rows = (
                await db.execute(
                    select(AgentSkillResource).where(AgentSkillResource.skill_id == clone_id)
                )
            ).scalars().all()
        assert {r.path: r.content for r in rows} == {p: c for p, (_i, c) in before.items()}
        async with sessions() as db:
            clone = await db.get(AgentSkill, clone_id)
            assert clone.license == next(s["license"] for s in CTI_SKILLS)


@pytest.mark.asyncio
async def test_a_skill_licence_is_read_from_its_frontmatter_served_and_edited():
    """The Agent Skills ``license`` field: kept as metadata (the Catalog tab),
    never in the body the agent loads."""
    async with _temp_database("cti_pack") as sessions:
        user = await _user(sessions)
        app = FastAPI()
        app.include_router(skills_api.router, prefix="/api/v1/skills")

        async def _get_db():
            async with sessions() as db:
                yield db
                await db.commit()

        app.dependency_overrides[deps.get_current_user] = lambda: user
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[deps.require_agentic_ai] = lambda: None
        app.dependency_overrides[deps.require_ai_enabled] = lambda: None
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        content = (
            "---\nname: triage-notes\ndescription: How to triage.\n"
            "license: Apache-2.0. Copyright 2026 Example Org.\n---\n# Triage\nDo it.\n"
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/v1/skills", json={"content": content})
            assert created.status_code == 201, created.text
            skill_id = created.json()["id"]
            assert created.json()["license"] == "Apache-2.0. Copyright 2026 Example Org."
            fetched = await client.get(f"/api/v1/skills/{skill_id}")
            assert fetched.json()["license"] == "Apache-2.0. Copyright 2026 Example Org."
            edited = await client.put(f"/api/v1/skills/{skill_id}", json={"license": "MIT"})
            assert edited.status_code == 200, edited.text
            assert edited.json()["license"] == "MIT"
            cleared = await client.put(f"/api/v1/skills/{skill_id}", json={"license": "  "})
            assert cleared.json()["license"] is None
            too_long = await client.put(f"/api/v1/skills/{skill_id}", json={"license": "x" * 1001})
            assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_the_catalog_tab_reads_a_skill_licence_under_the_skill_access_rule():
    async with _temp_database("cti_pack") as sessions:
        reader = await _user(sessions)
        stranger = await _user(sessions)
        async with sessions() as db:
            await seed_builtin_skills(db, platform_mode="xtm_one")
            cti = next(
                s
                for s in (await db.execute(select(AgentSkill))).scalars().all()
                if "slug:cti-ach" in (s.tags or [])
            )
            private = AgentSkill(name="Private", content="# p", user_id=stranger.id, license="MIT")
            db.add(private)
            await db.commit()
            cti_id, private_id = cti.id, private.id

        app = FastAPI()
        app.include_router(skills_api.router, prefix="/api/v1/skills")

        async def _get_db():
            async with sessions() as db:
                yield db
                await db.commit()

        app.dependency_overrides[deps.get_current_user] = lambda: reader
        app.dependency_overrides[get_db] = _get_db
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            shown = await client.get(f"/api/v1/skills/{cti_id}/license")
            hidden = await client.get(f"/api/v1/skills/{private_id}/license")
        assert shown.status_code == 200, shown.text
        assert shown.json() == {
            "license": next(s["license"] for s in CTI_SKILLS),
            "file_path": "LICENSE.md",
            "text": _LICENSE_BYTES.decode(),
            "truncated": False,
        }
        assert hidden.status_code == 404
