"""The ``fleets`` built-in suite on PostgreSQL: who may do what.

Every call runs through the real tools and the real fleet service, as the
caller a chat or an assignment run would be:

* **Own:** a user creates a fleet with its orchestrator and members in one
  call; the answer names the fleet's page; the agent's home fleet is set.
* **Shared:** a fleet shared with a group at ``config`` is managed by its
  members, one shared at ``view`` is only read.
* **Admin:** an administrator manages a colleague's fleet and creates a
  company-managed one; a non-admin cannot.
* **Refused otherwise:** a stranger cannot see a fleet, nor put an agent they
  do not manage in theirs; a refusal leaves nothing behind.

Every user but the admin is a NON-admin (``user_visible_filter`` short-circuits
for admins, so an admin-only test proves nothing).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.builtin_tools.fleets import build_fleet_tools
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database(
        "fleettools", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _agent(session, owner: User, name: str, *, company: bool = False) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        is_company_managed=company,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _call(user: User, tool: str, **args) -> str:
    _defs, executor = build_fleet_tools(str(user.id))
    return await executor.handler(tool)(**args)


async def _fleet_named(session, name: str) -> Fleet | None:
    # The tools commit in their own session: reload the fleet over what this
    # session holds, without expiring the users and agents the test still reads
    # (an expired attribute would lazy-load outside the async greenlet).
    statement = select(Fleet).where(Fleet.name == name).execution_options(populate_existing=True)
    return (await session.execute(statement)).scalar_one_or_none()


async def test_own_a_fleet_is_created_with_its_members_and_answers_its_page() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        ea = await _agent(session, alice, "EA")
        dev = await _agent(session, alice, "Dev")
        await session.commit()

        answer = await _call(
            alice, "create_fleet", name="CEO Office", orchestrator_agent_id=str(ea.id), member_agent_ids=[str(dev.id)]
        )
        assert "Fleet **CEO Office** created." in answer
        fleet = await _fleet_named(session, "CEO Office")
        assert fleet is not None
        assert f"/fleets/{fleet.id}" in answer
        assert fleet.orchestrator_agent_id == ea.id
        homes = (
            await session.execute(
                select(FleetMember.agent_id).where(FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_MEMBER)
            )
        ).scalars().all()
        assert set(homes) == {ea.id, dev.id}

        listed = await _call(alice, "list_fleets")
        assert "CEO Office" in listed and "orchestrator EA" in listed


async def test_refused_a_stranger_sees_nothing_and_cannot_borrow_an_agent() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        ea = await _agent(session, alice, "EA")
        await session.commit()
        await _call(alice, "create_fleet", name="Alice fleet")
        fleet = await _fleet_named(session, "Alice fleet")
        assert fleet is not None

        assert "not found or you don't have access" in await _call(bob, "get_fleet", fleet_id=str(fleet.id))
        assert "not found or you don't have access" in await _call(bob, "delete_fleet", fleet_id=str(fleet.id))
        assert "No fleet yet" in await _call(bob, "list_fleets")

        # Bob's own fleet cannot take Alice's agent: the whole call is undone.
        answer = await _call(bob, "create_fleet", name="Bob fleet", orchestrator_agent_id=str(ea.id))
        assert "not found or you don't have access" in answer
        assert await _fleet_named(session, "Bob fleet") is None


async def test_shared_config_manages_and_view_only_reads() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        carol = await _user(session, "carol")
        group = Group(id=uuid.uuid4(), name=f"team-{uuid.uuid4().hex[:6]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(user_id=carol.id, group_id=group.id))
        await session.commit()
        await _call(alice, "create_fleet", name="Shared fleet")
        fleet = await _fleet_named(session, "Shared fleet")
        assert fleet is not None

        fleet.shared_group_ids = [str(group.id)]
        fleet.shared_group_permissions = {str(group.id): "view"}
        await session.commit()
        assert "Fleet **Shared fleet**" in await _call(carol, "get_fleet", fleet_id=str(fleet.id))
        assert "permission" in await _call(carol, "update_fleet", fleet_id=str(fleet.id), name="Renamed")

        fleet = await _fleet_named(session, "Shared fleet")
        assert fleet is not None
        fleet.shared_group_permissions = {str(group.id): "config"}
        await session.commit()
        assert "updated" in await _call(carol, "update_fleet", fleet_id=str(fleet.id), name="Renamed")
        assert await _fleet_named(session, "Renamed") is not None


async def test_admin_manages_a_colleague_s_fleet_and_company_managed_is_admin_only() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        admin = await _user(session, "admin", admin=True)
        company_agent = await _agent(session, admin, "Company EA", company=True)
        await session.commit()
        await _call(alice, "create_fleet", name="Alice fleet")
        fleet = await _fleet_named(session, "Alice fleet")
        assert fleet is not None

        assert "updated" in await _call(admin, "update_fleet", fleet_id=str(fleet.id), description="Checked")
        assert "Only admins" in await _call(alice, "create_fleet", name="Company", is_company_managed=True)
        answer = await _call(
            admin, "create_fleet", name="Company", is_company_managed=True, orchestrator_agent_id=str(company_agent.id)
        )
        assert "created" in answer
        company = await _fleet_named(session, "Company")
        assert company is not None and company.is_company_managed
        assert "Only admins" in await _call(alice, "delete_fleet", fleet_id=str(company.id))
        assert "deleted" in await _call(admin, "delete_fleet", fleet_id=str(company.id))
        session.expire_all()
        assert await session.scalar(select(func.count(Fleet.id)).where(Fleet.name == "Company")) == 0


async def test_the_autonomy_level_is_lowered_and_audited_as_the_page_does() -> None:
    from app.models.audit_log import AuditLog

    async with _database() as session:
        alice = await _user(session, "alice")
        await session.commit()
        await _call(alice, "create_fleet", name="Level fleet")
        fleet = await _fleet_named(session, "Level fleet")
        assert fleet is not None
        fleet.autonomy_level = "medium"
        await session.commit()

        assert "Autonomy level: Medium (medium)" in await _call(alice, "get_fleet", fleet_id=str(fleet.id))
        assert "autonomy Medium (medium)" in await _call(alice, "list_fleets")
        lowered = await _call(alice, "update_fleet", fleet_id=str(fleet.id), autonomy_level="full_gates")
        assert "updated. Autonomy level applied: Full human gates (full_gates)." in lowered
        refused = await _call(alice, "update_fleet", fleet_id=str(fleet.id), autonomy_level="low")
        assert "only lower" in refused and "Why: the user is not an administrator." in refused
        fleet = await _fleet_named(session, "Level fleet")
        assert fleet is not None and fleet.autonomy_level == "full_gates"
        rows = (
            await session.execute(
                select(AuditLog.details).where(
                    AuditLog.entity_id == fleet.id, AuditLog.action == "autonomy_level_change"
                )
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0]["from"] == "medium" and rows[0]["to"] == "full_gates"
        assert rows[0]["via"] == "agent"


async def test_an_administrators_agent_sets_the_instructions_position_unannounced_by_default() -> None:
    """#10255: the initial reading is recorded without a line in the members' prompt, audited as the page does."""
    from app.models.audit_log import AuditLog

    async with _database() as session:
        alice = await _user(session, "alice")
        admin = await _user(session, "admin", admin=True)
        await session.commit()
        await _call(alice, "create_fleet", name="Office fleet")
        fleet = await _fleet_named(session, "Office fleet")
        assert fleet is not None
        fleet_id = fleet.id
        # session.expire_all() below would expire the callers too: refresh them before each call.

        # A manager's agent may not record it, even a lower one.
        assert "Only an administrator" in await _call(
            alice, "update_fleet", fleet_id=str(fleet.id), instructions_position="medium_low"
        )
        assert "Unknown instructions position" in await _call(
            admin, "update_fleet", fleet_id=str(fleet.id), instructions_position="somewhere"
        )
        assert "updated" in await _call(admin, "update_fleet", fleet_id=str(fleet.id), instructions_position="medium_low")
        session.expire_all()
        await session.refresh(admin)
        fleet = await _fleet_named(session, "Office fleet")
        assert fleet is not None
        assert fleet.instructions_position == "medium_low"
        assert not fleet.instructions_position_announced

        assert "updated" in await _call(
            admin, "update_fleet", fleet_id=str(fleet.id), instructions_position="low", announce_instructions=True
        )
        session.expire_all()
        await session.refresh(admin)
        fleet = await _fleet_named(session, "Office fleet")
        assert fleet is not None and fleet.instructions_position == "low" and fleet.instructions_position_announced

        assert "updated" in await _call(admin, "update_fleet", fleet_id=str(fleet.id), instructions_position="none")
        session.expire_all()
        fleet = await _fleet_named(session, "Office fleet")
        assert fleet is not None and fleet.instructions_position is None and not fleet.instructions_position_announced

        rows = (
            await session.execute(
                select(AuditLog.details)
                .where(AuditLog.entity_id == fleet_id, AuditLog.action == "instructions_position_change")
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
        assert [(r["from"], r["to"], r["announced"], r["via"]) for r in rows] == [
            (None, "medium_low", False, "agent"),
            ("medium_low", "low", True, "agent"),
            ("low", None, False, "agent"),
        ]


async def test_members_and_exposure_are_replaced_through_the_service_rules() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        ea = await _agent(session, alice, "EA")
        dev = await _agent(session, alice, "Dev")
        ux = await _agent(session, alice, "UX")
        await session.commit()
        await _call(alice, "create_fleet", name="Office", orchestrator_agent_id=str(ea.id))
        fleet = await _fleet_named(session, "Office")
        assert fleet is not None

        answer = await _call(
            alice, "set_fleet_members", fleet_id=str(fleet.id), member_agent_ids=[str(ea.id), str(dev.id)],
            connected=[{"agent_id": str(ux.id), "context_inclusion": "summary"}],
        )
        assert "Home members (2)" in answer and "Connected (1)" in answer
        # A connected agent cannot be exposed: the service refuses, nothing changes.
        refused = await _call(alice, "set_fleet_exposure", fleet_id=str(fleet.id), agent_ids=[str(ux.id)])
        assert "Nothing was saved" in refused
        exposed = await _call(alice, "set_fleet_exposure", fleet_id=str(fleet.id), agent_ids=[str(dev.id)])
        assert "Dev" in exposed and "exposed" in exposed


async def _parent_of(session, fleet: Fleet, agent: Agent) -> uuid.UUID | None:
    statement = (
        select(FleetMember)
        .where(FleetMember.fleet_id == fleet.id, FleetMember.agent_id == agent.id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one().parent_member_id


async def _member_id(session, fleet: Fleet, agent: Agent) -> uuid.UUID:
    statement = select(FleetMember.id).where(FleetMember.fleet_id == fleet.id, FleetMember.agent_id == agent.id)
    return (await session.execute(statement)).scalar_one()


async def test_a_member_s_parent_is_set_read_back_reset_and_refused_through_the_service_rules() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        ea = await _agent(session, alice, "EA")
        dev = await _agent(session, alice, "Dev")
        ux = await _agent(session, alice, "UX")
        outsider = await _agent(session, alice, "Outsider")
        await session.commit()
        await _call(
            alice, "create_fleet", name="Office", orchestrator_agent_id=str(ea.id),
            member_agent_ids=[str(dev.id), str(ux.id)],
        )
        fleet = await _fleet_named(session, "Office")
        assert fleet is not None
        fid = str(fleet.id)

        # Set: Dev reports to the orchestrator; get_fleet reads it back.
        answer = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id),
                             parent_agent_id=str(ea.id))
        assert "member parent set" in answer
        ea_member = await _member_id(session, fleet, ea)
        assert await _parent_of(session, fleet, dev) == ea_member
        read = await _call(alice, "get_fleet", fleet_id=fid)
        assert f"Dev `{dev.id}` (member `{await _member_id(session, fleet, dev)}`, reports to EA `{ea.id}`)" in read
        # A parentless member is drawn under the orchestrator (fleet_tree): the read-back says so.
        ux_member = await _member_id(session, fleet, ux)
        assert f"UX `{ux.id}` (member `{ux_member}`, top level, under the orchestrator)" in read
        assert f"EA `{ea.id}` (member `{ea_member}`, top level)" in read

        # UX under Dev, then Dev under UX would loop: refused, nothing written.
        await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(ux.id), parent_agent_id=str(dev.id))
        loop = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id),
                           parent_agent_id=str(ux.id))
        assert "would loop" in loop and "Nothing was saved" in loop
        assert await _parent_of(session, fleet, dev) == ea_member

        # Itself, a parent outside the fleet, the orchestrator as a child: refused.
        itself = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id),
                             parent_agent_id=str(dev.id))
        assert "cannot report to itself" in itself and "Nothing was saved" in itself
        foreign = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id),
                              parent_agent_id=str(outsider.id))
        assert "is not a home member of this fleet" in foreign and "Nothing was saved" in foreign
        root = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(ea.id),
                           parent_agent_id=str(dev.id))
        assert "orchestrator is the root" in root
        assert await _parent_of(session, fleet, ea) is None
        assert await _parent_of(session, fleet, dev) == ea_member

        # Reset: null puts Dev back at top level.
        await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id), parent_agent_id=None)
        assert await _parent_of(session, fleet, dev) is None

        # A stranger cannot move a member of Alice's fleet.
        bob = await _user(session, "bob")
        await session.commit()
        refused = await _call(bob, "set_fleet_member_parent", fleet_id=fid, agent_id=str(dev.id),
                              parent_agent_id=str(ea.id))
        assert "not found or you don't have access" in refused
        assert await _parent_of(session, fleet, dev) is None


async def _role_of(session, fleet: Fleet, agent: Agent) -> str:
    statement = (
        select(FleetMember)
        .where(FleetMember.fleet_id == fleet.id, FleetMember.agent_id == agent.id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one().role_label


async def test_a_member_s_role_label_is_set_read_back_cleared_and_refused_on_a_company_managed_fleet() -> None:
    from app.models.audit_log import AuditLog

    label = "Root orchestrator for threat research and correlation"
    async with _database() as session:
        admin = await _user(session, "admin", admin=True)
        alice = await _user(session, "alice")
        root = await _agent(session, admin, "Research EA", company=True)
        await session.commit()
        await _call(admin, "create_fleet", name="Research", is_company_managed=True, orchestrator_agent_id=str(root.id))
        fleet = await _fleet_named(session, "Research")
        assert fleet is not None and fleet.is_company_managed
        fid = str(fleet.id)
        assert await _role_of(session, fleet, root) == ""

        # Set: the answer names the old and the new label; get_fleet reads it back.
        answer = await _call(admin, "set_fleet_member_parent", fleet_id=fid, agent_id=str(root.id), role_label=label)
        assert f'role label of Research EA `{root.id}` set from none to "{label}"' in answer
        assert await _role_of(session, fleet, root) == label
        assert f"role label: {label}" in await _call(admin, "get_fleet", fleet_id=fid)
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.entity_id == fleet.id).order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert any((a.details or {}).get("role_label") == label for a in audit)

        # A user who may not manage the fleet is refused, nothing saved.
        refused = await _call(alice, "set_fleet_member_parent", fleet_id=fid, agent_id=str(root.id), role_label="Mine")
        assert "Only admins" in refused or "you don't have access" in refused or "permission" in refused
        assert await _role_of(session, fleet, root) == label

        # Clear with an empty string.
        cleared = await _call(admin, "set_fleet_member_parent", fleet_id=fid, agent_id=str(root.id), role_label="")
        assert f'set from "{label}" to none' in cleared
        assert await _role_of(session, fleet, root) == ""
