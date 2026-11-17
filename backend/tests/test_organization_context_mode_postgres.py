"""The organization context mode round trip (#11533), on a real PostgreSQL.

The platform tools write and read ``organization_context_mode`` on agents and
assignments (create, update, get shows the stored and the effective value),
refuse an unknown value, and the platform default comes from the migration's
``platform_settings.organization_context_enabled`` (true).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.platform_settings import PlatformSettings
from app.services.organization_context_injection import platform_enabled
from tests.test_agent_builder_versions_postgres import _builder, _only_agent_id, _user
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


async def _mode_of(sessionmaker, model, row_id) -> str:
    async with sessionmaker() as session:
        return (await session.execute(select(model.organization_context_mode).where(model.id == row_id))).scalar_one()


@pytest.mark.asyncio
async def test_agent_and_assignment_modes_round_trip_through_the_platform_tools():
    async with _temp_database("orgctx_mode") as sessionmaker:
        user = await _user(sessionmaker)

        await _builder(
            user.id, "create_agent", {"name": "Org reader", "persona": "One.", "organization_context_mode": "off"}
        )
        agent_id = await _only_agent_id(sessionmaker, user)
        assert await _mode_of(sessionmaker, Agent, agent_id) == "off"
        shown = await _builder(user.id, "get_agent", {"agent_id": str(agent_id)})
        assert "Organization context: off (effective: off)" in shown

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "organization_context_mode": "inherit"})
        assert await _mode_of(sessionmaker, Agent, agent_id) == "inherit"
        shown = await _builder(user.id, "get_agent", {"agent_id": str(agent_id)})
        assert "Organization context: inherit (effective: on)" in shown

        refused = await _builder(
            user.id, "update_agent", {"agent_id": str(agent_id), "organization_context_mode": "sometimes"}
        )
        assert "inherit, always, off" in refused
        assert await _mode_of(sessionmaker, Agent, agent_id) == "inherit"

        await _builder(
            user.id,
            "create_assignment",
            {
                "agent_id": str(agent_id),
                "name": "Weekly",
                "prompt": "Do it.",
                "trigger_type": "manual",
                "organization_context_mode": "always",
            },
        )
        async with sessionmaker() as session:
            assignment_id = (
                await session.execute(select(Assignment.id).where(Assignment.agent_id == agent_id))
            ).scalar_one()
        assert await _mode_of(sessionmaker, Assignment, assignment_id) == "always"

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "organization_context_mode": "off"})
        shown = await _builder(user.id, "get_assignment", {"assignment_id": str(assignment_id)})
        assert "Organization context: always (effective: on)" in shown, "the assignment wins over the agent"

        await _builder(
            user.id, "update_assignment", {"assignment_id": str(assignment_id), "organization_context_mode": "inherit"}
        )
        shown = await _builder(user.id, "get_assignment", {"assignment_id": str(assignment_id)})
        assert "Organization context: inherit (effective: off)" in shown


@pytest.mark.asyncio
async def test_the_platform_default_is_on_and_follows_the_setting():
    async with _temp_database("orgctx_platform") as sessionmaker, sessionmaker() as session:
        assert await platform_enabled(session) is True, "no settings row yet: on"
        row = PlatformSettings(id=uuid.uuid4())
        session.add(row)
        await session.commit()
        assert row.organization_context_enabled is True
        row.organization_context_enabled = False
        await session.commit()
        assert await platform_enabled(session) is False
