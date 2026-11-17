"""With ``CONCIERGE`` off, the Concierge's surfaces stay dark.

Pinned here: every built-in group gated on the Concierge (``gate="concierge"``)
is withheld from the offered catalog while the registry keeps it, and the agent
builder does not advertise the memory steward; a chat turn wires none of the
Concierge's tool groups; the improvement proposal routes answer 403; the
listing rule hides the seeded fleet (``concierge_role``), administrators
included, and shows it again once the flag is on.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.helpers import user_visible_filter
from app.core.config import settings
from app.models.agent import Agent
from app.models.user import User
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.tools.base import CompositeToolExecutor

_CONCIERGE_GROUPS = {group.id for group in BUILTIN_TOOL_GROUPS if group.gate == "concierge"}


def _flag(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE" if on else "")


def test_every_concierge_group_is_gated():
    assert {
        "concierge_sandbox",
        "concierge_evaluation",
        "improvement_proposals",
        "memory_steward",
        "concierge_orchestrator",
        "concierge_diagnosis",
        "concierge_experiments",
        "flow_lab",
    } <= _CONCIERGE_GROUPS


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_the_offered_catalog_follows_the_flag(monkeypatch, on):
    from app.api.v1.settings import _offered_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog
    from app.services.builtin_tools import agent_builder

    _flag(monkeypatch, on)

    offered = set(_offered_builtin_tool_catalog())
    assert not on or offered >= _CONCIERGE_GROUPS
    assert on or not _CONCIERGE_GROUPS & offered
    # The registry keeps them: an agent already carrying one stays valid.
    assert set(build_builtin_tool_catalog()) >= _CONCIERGE_GROUPS
    assert ("memory_steward" in agent_builder._advertised_builtin_tool_ids()) is on


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_a_chat_turn_wires_the_concierge_groups_only_with_the_flag_on(monkeypatch, on):
    from app.services.agent_chat import _add_platform_builder_tools

    _flag(monkeypatch, on)
    composite = CompositeToolExecutor()
    _add_platform_builder_tools(
        composite, None, str(uuid.uuid4()), ["improvement_proposals", "concierge_sandbox", "concierge_evaluation"]
    )

    names = set(composite.tool_names)
    assert ({"list_improvements", "propose_improvement"} <= names) is on
    assert bool(names) is on


@pytest.mark.parametrize(
    ("features", "served"),
    [("", False), ("CONCIERGE", True)],
    ids=["both-off", "concierge-on"],
)
def test_the_proposal_routes_are_served_only_while_a_proposal_is_reachable(monkeypatch, features, served):
    from app.api.v1.improvement_proposals import require_surface

    monkeypatch.setattr(settings, "enabled_features", features)
    if served:
        require_surface()
        return
    with pytest.raises(HTTPException) as refused:
        require_surface()
    assert refused.value.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_fleet_is_listed_to_nobody_while_the_flag_is_off(monkeypatch):
    from tests.test_provisioning_postgres import _temp_database

    async with _temp_database("concierge_flag_off_fleet") as sessionmaker:
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
                concierge_role="orchestrator",
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

        _flag(monkeypatch, False)
        assert await _listed(user) == {plain.id}
        assert await _listed(admin) == {plain.id}
        _flag(monkeypatch, True)
        assert await _listed(user) == {plain.id, fleet.id}
