"""A configured handover is the grant to its peer (#1967).

Person P may open agent A, not agent B; A holds a consult handover to B.
P's turn with A loads B as a peer (consult_agent reaches it), the same
filter that keeps P from opening B anywhere else is unchanged, and a
non-exposed member of another fleet stays out of reach.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.services.fleets import configured_handover_reach, handover_peer_clause
from tests.test_work_item_handed_to_owner_postgres import _fleet, _private_agent
from tests.test_work_item_workspace_scope_postgres import _make_user, _temp_database

pytestmark = pytest.mark.integration


async def _peers(session: Any, source: Any, person: Any) -> set[uuid.UUID]:
    from app.models.agent import Agent

    granted = await configured_handover_reach(session, source.id, source.handover_agent_ids)
    rows = await session.execute(
        select(Agent.id).where(
            Agent.id.in_([uuid.UUID(h) for h in source.handover_agent_ids]),
            handover_peer_clause(person.id, [], granted),
        )
    )
    return set(rows.scalars().all())


async def _visible(session: Any, agent_id: uuid.UUID, person: Any) -> bool:
    from app.api.v1.helpers import user_visible_filter
    from app.models.agent import Agent

    row = await session.execute(select(Agent.id).where(Agent.id == agent_id, user_visible_filter(Agent, person.id, [])))
    return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_a_configured_peer_is_loaded_for_a_person_who_cannot_open_it() -> None:
    async with _temp_database() as sessions, sessions() as session:
        person = await _make_user(session, email="p@example.com")
        holder = await _make_user(session, email="b-owner@example.com")
        source = await _private_agent(session, person, name="Agent A")
        peer = await _private_agent(session, holder, name="Agent B")
        stranger = await _private_agent(session, holder, name="Agent C")
        source.handover_agent_ids = [str(peer.id)]
        source.handover_types = {str(peer.id): "consult"}
        await session.commit()

        loaded = await _peers(session, source, person)
        # P still cannot open B, nor any agent no handover names.
        opens_peer = await _visible(session, peer.id, person)
        opens_stranger = await _visible(session, stranger.id, person)

    assert loaded == {peer.id}
    assert not opens_peer
    assert not opens_stranger


@pytest.mark.asyncio
async def test_without_the_handover_nothing_is_granted() -> None:
    async with _temp_database() as sessions, sessions() as session:
        person = await _make_user(session, email="p2@example.com")
        source = await _private_agent(session, person, name="Agent A2")
        source.handover_agent_ids = []
        await session.commit()

        granted = await configured_handover_reach(session, source.id, source.handover_agent_ids)

    assert granted == set()


@pytest.mark.asyncio
async def test_a_non_exposed_member_of_another_fleet_is_not_granted() -> None:
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    async with _temp_database() as sessions, sessions() as session:
        person = await _make_user(session, email="p3@example.com")
        holder = await _make_user(session, email="fleet3@example.com")
        source = await _private_agent(session, person, name="Agent A3")
        orchestrator = await _private_agent(session, holder, name="Orchestrator")
        hidden = await _private_agent(session, holder, name="Hidden")
        fleet = await _fleet(session, holder, orchestrator, name="Other")
        fleet.orchestrator_agent_id = orchestrator.id
        session.add(FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=hidden.id, role=FLEET_ROLE_MEMBER))
        source.handover_agent_ids = [str(orchestrator.id), str(hidden.id)]
        await session.commit()

        loaded = await _peers(session, source, person)

    assert loaded == {orchestrator.id}
