"""A work item one person hands to another person's agent (#858).

The item carries its creator's ``user_id``; the owner agent's runs act as
the owner agent's own person. Sam's decision on #858: the owner agent's
runs read, list and claim such an item when (1) the creator may open the
owner agent (shared with them), or (2) the creating agent's home fleet and
the owner agent's home fleet are connected; Sam's decision on #1967 adds (3)
the creating agent holds a configured handover to the owner agent, the
exposure rule kept. Any other hand-off is refused at creation with the
owner agent's name, never written and dropped.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration


async def _private_agent(session: Any, owner: Any, *, name: str, shared_with: list[Any] | None = None) -> Any:
    agent = await _make_agent(session, name=name)
    agent.user_id = owner.id
    agent.is_company_managed = False
    agent.shared_user_ids = [str(u.id) for u in shared_with or []]
    await session.flush()
    return agent


async def _fleet(session: Any, owner: Any, home_agent: Any, *, name: str) -> Any:
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
    from app.models.workspace import Workspace

    workspace = Workspace(
        id=uuid.uuid4(), name=f"{name} workspace", slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}", user_id=owner.id
    )
    session.add(workspace)
    await session.flush()
    fleet = Fleet(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower().replace(" ", "-"),
        workspace_id=workspace.id,
        user_id=owner.id,
    )
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=home_agent.id, role=FLEET_ROLE_MEMBER))
    await session.flush()
    return fleet


async def _hand(*, creator: Any, creating_agent: Any, owner_agent: Any, title: str) -> dict[str, Any]:
    return await wi_service.create_work_item(
        title=title,
        user_id=str(creator.id),
        owner_agent_id=str(owner_agent.id),
        created_by_agent_id=str(creating_agent.id),
        workspace_id=None,
        writer=agent_writer(is_company_managed=False, agent_id=creating_agent.id),
    )


@pytest.mark.asyncio
async def test_a_shared_owner_agent_reads_lists_and_claims_the_hand_off() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            creator = await _make_user(session, email="creator@example.com")
            holder = await _make_user(session, email="holder@example.com")
            creating = await _private_agent(session, creator, name="Creator desk")
            owner = await _private_agent(session, holder, name="Holder assistant", shared_with=[creator])
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _hand(creator=creator, creating_agent=creating, owner_agent=owner, title="shared")
            reader = agent_writer(is_company_managed=False, agent_id=owner.id)
            read = await wi_service.get_work_item(item["id"], user_id=str(holder.id), reader=reader)
            listed = await wi_service.list_work_items(
                user_id=str(holder.id), reader=reader, owner_agent_id=str(owner.id)
            )
            claimed = await wi_service.claim_work_item(
                item["id"], agent_id=str(owner.id), user_id=str(holder.id), writer=reader
            )
            # A person's own read gains nothing: only the owner agent's path widens.
            person = await wi_service.get_work_item(item["id"], user_id=str(holder.id))

    assert read is not None and read["id"] == item["id"]
    assert item["id"] in {i["id"] for i in listed}
    assert claimed is not None
    assert person is None


@pytest.mark.asyncio
async def test_connected_fleets_let_the_owner_agent_read_the_hand_off() -> None:
    from app.models.fleet import FleetMember

    async with _temp_database() as sessions:
        async with sessions() as session:
            creator = await _make_user(session, email="dso@example.com")
            holder = await _make_user(session, email="office@example.com")
            creating = await _private_agent(session, creator, name="DSO assistant")
            owner = await _private_agent(session, holder, name="Office assistant")
            dso = await _fleet(session, creator, creating, name="DSO")
            office = await _fleet(session, holder, owner, name="Office")
            # The owner's fleet names the creator's fleet as connected.
            session.add(FleetMember(id=uuid.uuid4(), fleet_id=office.id, target_fleet_id=dso.id, role="connected"))
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _hand(creator=creator, creating_agent=creating, owner_agent=owner, title="connected")
            reader = agent_writer(is_company_managed=False, agent_id=owner.id)
            read = await wi_service.get_work_item(item["id"], user_id=str(holder.id), reader=reader)

    assert read is not None and read["id"] == item["id"]


@pytest.mark.asyncio
async def test_the_owner_agent_lists_hand_offs_from_two_connected_fleets() -> None:
    """Two items handed by agents of two fleets: the home-fleet subquery stays per item (#890).

    Before the fix the subquery lost its correlation, read every item's
    creator and the listing raised CardinalityViolationError.
    """
    from app.models.fleet import FleetMember

    async with _temp_database() as sessions:
        async with sessions() as session:
            first_creator = await _make_user(session, email="first@example.com")
            second_creator = await _make_user(session, email="second@example.com")
            holder = await _make_user(session, email="holder2@example.com")
            first = await _private_agent(session, first_creator, name="First desk")
            second = await _private_agent(session, second_creator, name="Second desk")
            owner = await _private_agent(session, holder, name="Holder desk")
            first_fleet = await _fleet(session, first_creator, first, name="First")
            second_fleet = await _fleet(session, second_creator, second, name="Second")
            office = await _fleet(session, holder, owner, name="Holder")
            for target in (first_fleet, second_fleet):
                session.add(
                    FleetMember(id=uuid.uuid4(), fleet_id=office.id, target_fleet_id=target.id, role="connected")
                )
            await session.commit()

        async with _pointed_at_db(sessions):
            one = await _hand(creator=first_creator, creating_agent=first, owner_agent=owner, title="one")
            two = await _hand(creator=second_creator, creating_agent=second, owner_agent=owner, title="two")
            reader = agent_writer(is_company_managed=False, agent_id=owner.id)
            listed = await wi_service.list_work_items(user_id=str(holder.id), reader=reader)

    assert {one["id"], two["id"]} <= {i["id"] for i in listed}


@pytest.mark.asyncio
async def test_a_group_share_reaches_only_items_its_members_created() -> None:
    """The group grant reads the item's own creator, not any item's (#890)."""
    from app.models.group import Group, GroupMember

    async with _temp_database() as sessions:
        async with sessions() as session:
            member = await _make_user(session, email="member@example.com")
            outsider = await _make_user(session, email="outsider@example.com")
            holder = await _make_user(session, email="holder3@example.com")
            group = Group(id=uuid.uuid4(), name="Shared group")
            session.add(group)
            await session.flush()
            session.add(GroupMember(group_id=group.id, user_id=member.id))
            member_desk = await _private_agent(session, member, name="Member desk")
            outsider_desk = await _private_agent(session, outsider, name="Outsider desk", shared_with=[outsider])
            owner = await _private_agent(session, holder, name="Group-shared assistant", shared_with=[outsider])
            owner.shared_group_ids = [str(group.id)]
            await session.commit()

        async with _pointed_at_db(sessions):
            mine = await _hand(creator=member, creating_agent=member_desk, owner_agent=owner, title="by member")
            theirs = await _hand(creator=outsider, creating_agent=outsider_desk, owner_agent=owner, title="by outsider")
            async with sessions() as session:
                from app.models.agent import Agent

                # The outsider loses the person share: only the group grant
                # could reach their item now, and they are not in the group.
                agent = await session.get(Agent, owner.id)
                agent.shared_user_ids = []
                await session.commit()
            reader = agent_writer(is_company_managed=False, agent_id=owner.id)
            listed = {i["id"] for i in await wi_service.list_work_items(user_id=str(holder.id), reader=reader)}

    assert mine["id"] in listed
    assert theirs["id"] not in listed


@pytest.mark.asyncio
async def test_a_configured_handover_lets_the_owner_agent_read_claim_and_complete() -> None:
    """Grant 3 (#1967): the creating agent's handover reaches an agent the person cannot open."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            creator = await _make_user(session, email="legal@example.com")
            holder = await _make_user(session, email="dso-owner@example.com")
            creating = await _private_agent(session, creator, name="Legal assistant")
            owner = await _private_agent(session, holder, name="DSO assistant")
            creating.handover_agent_ids = [str(owner.id)]
            creating.handover_types = {str(owner.id): "consult"}
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _hand(creator=creator, creating_agent=creating, owner_agent=owner, title="handover")
            reader = agent_writer(is_company_managed=False, agent_id=owner.id)
            listed = await wi_service.list_work_items(
                user_id=str(holder.id), reader=reader, owner_agent_id=str(owner.id)
            )
            claimed = await wi_service.claim_work_item(
                item["id"], agent_id=str(owner.id), user_id=str(holder.id), writer=reader
            )
            done = await wi_service.complete_work_item(
                item["id"], user_id=str(holder.id), result="answered", writer=reader
            )
            # The person gains nothing on the owner agent's items by the grant.
            person = await wi_service.get_work_item(item["id"], user_id=str(holder.id))

    assert item["id"] in {i["id"] for i in listed}
    assert claimed is not None
    assert done is not None and done["status"] == "done"
    assert person is None


@pytest.mark.asyncio
async def test_a_handover_to_a_non_exposed_member_of_another_fleet_is_still_refused() -> None:
    """The exposure rule wins over grant 3: a hidden member stays out of reach."""
    async with _temp_database() as sessions:
        async with sessions() as session:
            creator = await _make_user(session, email="outside@example.com")
            holder = await _make_user(session, email="fleet-owner@example.com")
            creating = await _private_agent(session, creator, name="Outside desk")
            orchestrator = await _private_agent(session, holder, name="Fleet orchestrator")
            hidden = await _private_agent(session, holder, name="Hidden member")
            fleet = await _fleet(session, holder, orchestrator, name="Closed")
            fleet.orchestrator_agent_id = orchestrator.id
            from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

            session.add(FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=hidden.id, role=FLEET_ROLE_MEMBER))
            creating.handover_agent_ids = [str(hidden.id), str(orchestrator.id)]
            await session.commit()

        async with _pointed_at_db(sessions):
            with pytest.raises(wi_service.OwnerAgentUnreachableError, match="Hidden member"):
                await _hand(creator=creator, creating_agent=creating, owner_agent=hidden, title="hidden")
            # The orchestrator is always exposed: the same handover reaches it.
            item = await _hand(creator=creator, creating_agent=creating, owner_agent=orchestrator, title="entry")

    assert item["title"] == "entry"


@pytest.mark.asyncio
async def test_an_unreachable_owner_agent_is_refused_by_name() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            creator = await _make_user(session, email="stranger@example.com")
            holder = await _make_user(session, email="private@example.com")
            creating = await _private_agent(session, creator, name="Stranger desk")
            owner = await _private_agent(session, holder, name="Private assistant")
            await session.commit()

        async with _pointed_at_db(sessions):
            with pytest.raises(wi_service.OwnerAgentUnreachableError, match="Private assistant"):
                await _hand(creator=creator, creating_agent=creating, owner_agent=owner, title="refused")
            mine = await wi_service.list_work_items(user_id=str(creator.id))

    assert not [i for i in mine if i["title"] == "refused"]
