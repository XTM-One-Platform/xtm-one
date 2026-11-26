"""Who a company-managed agent's decision waits for when nobody is named (#2367).

A company-managed agent's ``user_id`` is the seed account
(``resolve_company_managed_owner_id``): it stands for the platform, not for a
decider. A decision such an agent files with no recipient, in a run that acts
for no person (a webhook or an event), went to that account and waited for
nobody. It now goes to the CTEM ``decide`` groups for CTEM work, to the owner
of the agent's home fleet when a person owns it, else to the administrators;
personal agents and explicit recipients are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.services.work_item as wi_service
from app.agents.openaev_detection_desk import LAUNCH_PREFIX, ORCHESTRATOR, REMEDIATION_PREFIX
from app.models.fleet import Fleet, FleetMember
from app.models.group import Group, GroupMember
from app.services.company_managed_writes import agent_writer
from app.services.workspaces import create_workspace
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Launch the simulation now?",
    "options": [
        {"key": "A", "label": "Launch"},
        {"key": "B", "label": "Not now"},
    ],
    "recommendation": "A",
}


async def _decision(agent: Any, *, key: str | None = None, recipient: str | None = None) -> dict[str, Any]:
    """A needs_human item a company-managed agent files in a run that acts for no person."""
    return await wi_service.create_work_item(
        user_id=None,
        title="decide",
        owner_agent_id=str(agent.id),
        created_by_agent_id=str(agent.id),
        status="blocked",
        needs_human=True,
        ask=_ASK,
        recipient=recipient,
        correlation_key=key,
        is_company_managed=True,
        writer=agent_writer(is_company_managed=True, agent_id=agent.id),
    )


async def _company_agent(session: Any, seed_owner: Any, *, slug: str | None = None) -> Any:
    agent = await _make_agent(session)
    agent.user_id = seed_owner.id
    agent.is_company_managed = True
    if slug:
        agent.slug = slug
        agent.is_builtin = True
    return agent


async def _home_fleet(session: Any, agent: Any, *, owner_id: uuid.UUID, slug: str, builtin: bool) -> Fleet:
    workspace = await create_workspace(session, user_id=owner_id, name=f"{slug} home")
    fleet = Fleet(
        user_id=owner_id,
        slug=slug,
        name=slug,
        workspace_id=workspace.id,
        orchestrator_agent_id=agent.id,
        is_builtin=builtin,
    )
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(fleet_id=fleet.id, agent_id=agent.id, role="member"))
    return fleet


async def _decide_group(session: Any, member: Any, *, notify: bool = True) -> Group:
    group = Group(id=uuid.uuid4(), name=f"deciders-{uuid.uuid4().hex[:6]}", ctem_access="decide", ctem_notify=notify)
    session.add(group)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=member.id))
    return group


@pytest.mark.asyncio
async def test_a_company_agent_decision_waits_for_the_administrators_never_the_seed_owner() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed@example.com")
            admin = await _make_user(session, email="admin@example.com", is_admin=True)
            agent = await _company_agent(session, seed_owner)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)
            seed = await wi_service.work_items_waiting_for_person(user_id=str(seed_owner.id))
            admins = await wi_service.work_items_waiting_for_person(user_id=str(admin.id))

    assert item["waits_for_user_id"] is None
    assert not item.get("waits_for_group_ids")
    assert seed["count"] == 0
    assert admins["count"] == 1


@pytest.mark.asyncio
async def test_a_company_agent_in_a_ctem_fleet_waits_for_the_decide_groups() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed2@example.com")
            await _make_user(session, email="admin2@example.com", is_admin=True)
            decider = await _make_user(session, email="decider@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=seed_owner.id, slug="ctem-loop", builtin=True)
            group = await _decide_group(session, decider)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)
            waits = await wi_service.work_items_waiting_for_person(user_id=str(decider.id))
            seed = await wi_service.work_items_waiting_for_person(user_id=str(seed_owner.id))

    assert item["waits_for_user_id"] is None
    assert item["waits_for_group_ids"] == [str(group.id)]
    assert waits["count"] == 1
    assert seed["count"] == 0


@pytest.mark.asyncio
async def test_a_ctem_company_agent_with_no_notified_decide_group_waits_for_the_administrators() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed3@example.com")
            await _make_user(session, email="admin3@example.com", is_admin=True)
            quiet = await _make_user(session, email="quiet@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=seed_owner.id, slug="ctem-loop", builtin=True)
            await _decide_group(session, quiet, notify=False)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)

    assert item["waits_for_user_id"] is None
    assert not item.get("waits_for_group_ids")


@pytest.mark.asyncio
async def test_a_company_agent_in_a_persons_fleet_waits_for_the_fleet_owner() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed4@example.com")
            await _make_user(session, email="admin4@example.com", is_admin=True)
            fleet_owner = await _make_user(session, email="fleet-owner@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=fleet_owner.id, slug="my-desk", builtin=False)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)

    assert item["waits_for_user_id"] == str(fleet_owner.id)


@pytest.mark.asyncio
async def test_personal_agents_and_explicit_recipients_are_unchanged() -> None:
    """A company-managed agent names a decider (an administrator here); any person since #2422 is not kept."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = await _make_user(session, email="personal-owner@example.com")
            chosen = await _make_user(session, email="admin5@example.com", is_admin=True)
            seed_owner = await _make_user(session, email="seed5@example.com")
            personal = await _make_agent(session, name="Personal")
            personal.user_id = owner.id
            company = await _company_agent(session, seed_owner)
            await session.commit()

        async with _pointed_at_db(sessions):
            mine = await wi_service.create_work_item(
                user_id=None,
                title="decide",
                owner_agent_id=str(personal.id),
                created_by_agent_id=str(personal.id),
                status="blocked",
                needs_human=True,
                ask=_ASK,
                is_company_managed=True,
                writer=agent_writer(is_company_managed=True, agent_id=personal.id),
            )
            named = await _decision(company, recipient=str(chosen.id))

    assert mine["waits_for_user_id"] == str(owner.id)
    assert named["waits_for_user_id"] == str(chosen.id)


@pytest.mark.asyncio
async def test_detection_desk_gap_and_launch_gates_reach_the_decide_groups_on_an_event_run() -> None:
    """Regression on ``openaev_detection_desk``: its gates are filed with no recipient from webhook runs."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed6@example.com")
            await _make_user(session, email="admin6@example.com", is_admin=True)
            decider = await _make_user(session, email="decider6@example.com")
            orchestrator = await _company_agent(session, seed_owner, slug=ORCHESTRATOR)
            # The desk is folded into the seeded CTEM loop (builtin_fleets ``absorbs``).
            await _home_fleet(session, orchestrator, owner_id=seed_owner.id, slug="ctem-loop", builtin=True)
            group = await _decide_group(session, decider)
            await session.commit()

        async with _pointed_at_db(sessions):
            launch = await _decision(orchestrator, key=f"{LAUNCH_PREFIX}sim-1")
            gap = await _decision(orchestrator, key=f"{REMEDIATION_PREFIX}T1059-host-1")
            waits = await wi_service.work_items_waiting_for_person(user_id=str(decider.id))

    for item in (launch, gap):
        assert item["waits_for_group_ids"] == [str(group.id)]
        assert item["waits_for_user_id"] is None
    assert waits["count"] == 2
