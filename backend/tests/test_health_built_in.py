"""Platform Health is built in (#101 S1): no ``CONCIERGE`` flag gates its surfaces.

Pinned here, with ``ENABLED_FEATURES`` empty: every built-in group gated on the
Concierge (``gate="health"``) is offered and kept by the registry, and the
agent builder advertises the memory steward; a chat turn wires the Concierge's
tool groups; the improvement proposal routes are served; the listing rule shows
the seeded fleet (``health_role``) to everyone the listing allows.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.v1.helpers import user_visible_filter
from app.core.config import settings
from app.models.agent import Agent
from app.models.user import User
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.tools.base import CompositeToolExecutor

_CONCIERGE_GROUPS = {group.id for group in BUILTIN_TOOL_GROUPS if group.gate == "health"}


@pytest.fixture(autouse=True)
def _no_flags(monkeypatch) -> None:
    monkeypatch.setattr(settings, "enabled_features", "")


def test_every_concierge_group_is_gated():
    # One group a person sees, one internal group the fleet carries (#1943).
    assert {"health_orchestrator", "health_specialists"} == _CONCIERGE_GROUPS


def test_the_offered_catalog_carries_the_concierge_groups_with_no_flag():
    from app.api.v1.settings import _offered_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog
    from app.services.builtin_tools import agent_builder

    assert set(_offered_builtin_tool_catalog()) >= _CONCIERGE_GROUPS
    assert set(build_builtin_tool_catalog()) >= _CONCIERGE_GROUPS
    assert "health_orchestrator" in agent_builder._advertised_builtin_tool_ids()
    assert "health_specialists" not in agent_builder._advertised_builtin_tool_ids()


def test_a_chat_turn_wires_the_concierge_groups_with_no_flag():
    from app.services.agent_chat import _add_platform_builder_tools

    composite = CompositeToolExecutor()
    _add_platform_builder_tools(composite, None, str(uuid.uuid4()), ["health_specialists", "health_orchestrator"])

    assert {"list_health_signals", "request_health_cycle"} <= set(composite.tool_names)


def test_the_proposal_routes_are_served_with_no_flag():
    from app.api.v1.improvement_proposals import require_surface

    require_surface()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_fleet_is_listed_with_no_flag():
    from tests.test_provisioning_postgres import _temp_database

    async with _temp_database("concierge_built_in_fleet") as sessionmaker:
        async with sessionmaker() as db:
            admin = User(
                id=uuid.uuid4(), email="admin-fleet@test.local", full_name="A", hashed_password="x", role="admin"
            )
            user = User(id=uuid.uuid4(), email="user-fleet@test.local", full_name="U", hashed_password="x", role="user")
            db.add_all([admin, user])
            await db.flush()
            fleet = Agent(
                id=uuid.uuid4(),
                name="Concierge",
                user_id=admin.id,
                is_company_managed=True,
                health_role="orchestrator",
                slug=f"concierge-{uuid.uuid4().hex[:6]}",
            )
            plain = Agent(
                id=uuid.uuid4(),
                name="Invoices",
                user_id=admin.id,
                is_company_managed=True,
                slug=f"invoices-{uuid.uuid4().hex[:6]}",
            )
            db.add_all([fleet, plain])
            await db.commit()

        async def _listed(person: User) -> set[uuid.UUID]:
            async with sessionmaker() as db:
                clause = user_visible_filter(Agent, person.id, [], is_admin=person.role == "admin")
                return set((await db.execute(select(Agent.id).where(clause))).scalars())

        assert await _listed(user) == {plain.id, fleet.id}
        assert await _listed(admin) == {plain.id, fleet.id}
