"""The personal-tools catalog switch against real PostgreSQL.

- The migration keeps the agents already published including their personal
  tools (today's behaviour), leaves every other agent off (a new publish), and
  a re-run never switches back on a publisher who turned it off.
- A NON-admin owner changes the switch on their own published agent through
  the agent update route, and another NON-admin's download follows it.

``integration``-marked and skipped when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.api.v1 import agents as agents_api
from app.api.v1 import public_catalog
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.models.agent import Agent
from app.models.custom_tool import CustomTool
from app.models.user import User
from tests.test_a2a_hooks_postgres import _temp_database

pytestmark = pytest.mark.integration

REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "c4249tools01_add_agent_catalog_include_personal_tools.py"
)


def _run_upgrade(sync_conn) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location(REVISION.stem, REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_conn)):
        module.upgrade()


def _user(name: str, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


@pytest.mark.asyncio
async def test_the_migration_keeps_published_agents_including_their_tools():
    async with _temp_database() as sm:
        async with sm() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            published = Agent(user_id=owner.id, name="Published", is_public_catalog=True)
            private = Agent(user_id=owner.id, name="Private", is_public_catalog=False)
            db.add_all([published, private])
            await db.commit()
            published_id, private_id = published.id, private.id

        engine = sm.kw["bind"]
        # The state an existing install is in before the revision: no column.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE agents DROP COLUMN catalog_include_personal_tools")
            )
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async def _values():
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT id, catalog_include_personal_tools FROM agents")
                )
                return {row[0]: row[1] for row in rows}

        assert await _values() == {published_id: True, private_id: False}

        # The publisher turns it off; a re-run of the revision is a no-op.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE agents SET catalog_include_personal_tools = false WHERE id = :id"),
                {"id": published_id},
            )
            await conn.run_sync(_run_upgrade)
        assert (await _values())[published_id] is False

        # A new agent (a later publish) starts off.
        async with sm() as db:
            fresh = Agent(user_id=None, name="Later", is_public_catalog=True)
            db.add(fresh)
            await db.commit()
            assert fresh.catalog_include_personal_tools is False


@pytest.mark.asyncio
async def test_a_non_admin_publisher_decides_and_a_non_admin_reader_follows(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_api, "log_audit", _noop)
    async with _temp_database() as sm:
        async with sm() as db:
            owner, reader = _user("owner"), _user("reader")
            db.add_all([owner, reader])
            await db.flush()
            tool = CustomTool(
                user_id=owner.id, name="My scraper", description="d", js_function="return 1"
            )
            db.add(tool)
            await db.flush()
            agent = Agent(
                user_id=owner.id,
                name="Published",
                slug=f"published-{uuid.uuid4().hex[:8]}",
                is_public_catalog=True,
                custom_tool_ids=[str(tool.id)],
            )
            db.add(agent)
            await db.commit()
            agent_id, owner_id, reader_id = agent.id, owner.id, reader.id

        async def _download_names():
            async with sm() as db:
                user = await db.get(User, reader_id)
                response = await public_catalog.download_catalog_item(
                    entity_type="agents", entity_id=str(agent_id), user=user, db=db
                )
            return [t["name"] for t in json.loads(response.body)["custom_tools"]]

        assert await _download_names() == []

        async with sm() as db:
            user = await db.get(User, owner_id)
            response = await agents_api.update_agent(
                str(agent_id),
                UpdateAgentRequest(catalog_include_personal_tools=True),
                SimpleNamespace(headers={}, client=None),
                user=user,
                db=db,
                _ai_gate=None,
            )
        assert response.catalog_include_personal_tools is True
        async with sm() as db:
            stored = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
            assert stored.catalog_include_personal_tools is True

        assert await _download_names() == ["My scraper"]
