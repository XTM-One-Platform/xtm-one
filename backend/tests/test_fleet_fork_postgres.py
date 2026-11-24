"""Fleet fork on PostgreSQL (#9174): an editable personal copy of any fleet.

* **Copy:** every member becomes a personal copy, the hierarchy, exposure,
  role labels and orchestrator follow, the intra-fleet handovers point at the
  copies, the provenance is recorded and the source is untouched.
* **Nothing runs:** every copied assignment is switched off with the
  ``created_disabled`` stamp and without the source's provisioning state,
  whatever the source's state (the opt-in tripwire of #9160, extended).
* **Built-in:** a built-in fleet forks into a normal fleet the seed never owns.
* **Keep:** the orchestrator is copied, the other members are connected.
* **Fingerprint:** a change of the source after the fork is detected.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.user import User
from app.services.builtin_tools.fleets import build_fleet_tools
from app.services.fleet_fork import fork_fleet, source_changed, source_changes
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with (
        scratch_database("fleetfork", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
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


async def _agent(session, owner: User, name: str, **fields) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        persona=f"You are {name}.",
        **fields,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _call(user: User, tool: str, **args) -> str:
    _defs, executor = build_fleet_tools(str(user.id))
    return await executor.handler(tool)(**args)


async def _office(session, alice: User) -> tuple[Fleet, list[Agent]]:
    """Orchestrator EA over Dev, UX (under Dev) and Analyst; EA hands over to Dev and UX."""
    dev = await _agent(session, alice, "Dev")
    ux = await _agent(session, alice, "UX")
    analyst = await _agent(session, alice, "Analyst")
    ea = await _agent(
        session,
        alice,
        "EA",
        handover_agent_ids=[str(dev.id), str(ux.id)],
        handover_types={str(dev.id): "delegate", str(ux.id): "consult"},
    )
    session.add_all(
        [
            Assignment(
                agent_id=ea.id,
                user_id=alice.id,
                name="Daily brief",
                trigger_type="schedule",
                trigger_config={"cron": "0 7 * * *", "opencti_provisioning": {"trigger_id": "source-owned"}},
                prompt="brief",
                enabled=True,
            ),
            Assignment(
                agent_id=dev.id, user_id=alice.id, name="On event", trigger_type="manual", prompt="go", enabled=True
            ),
        ]
    )
    await session.commit()
    answer = await _call(
        alice,
        "create_fleet",
        name="Office",
        orchestrator_agent_id=str(ea.id),
        member_agent_ids=[str(dev.id), str(ux.id), str(analyst.id)],
        exposed_agent_ids=[str(dev.id)],
    )
    fleet_id = re.search(r"ID: `([0-9a-f-]{36})`", answer).group(1)  # type: ignore[union-attr]
    await _call(alice, "set_fleet_member_parent", fleet_id=fleet_id, agent_id=str(ux.id), parent_agent_id=str(dev.id))
    fleet = await session.get(Fleet, uuid.UUID(fleet_id))
    await session.refresh(fleet)
    return fleet, [ea, dev, ux, analyst]


async def _rows(session, fleet_id: uuid.UUID) -> list[FleetMember]:
    return list((await session.execute(select(FleetMember).where(FleetMember.fleet_id == fleet_id))).scalars().all())


async def test_a_copy_fork_copies_structure_repoints_handovers_and_switches_every_assignment_off() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, (ea, dev, ux, analyst) = await _office(session, alice)

        result = await fork_fleet(session, source, alice, [], name="My office")
        await session.commit()
        fork = result.fleet

        assert fork.name == "My office"
        assert fork.id != source.id and not fork.is_builtin
        assert fork.forked_from["fleet_id"] == str(source.id)
        assert fork.forked_from["members"] == "copy"
        assert len(fork.forked_from["fingerprint"]) == 16

        copies = result.copied_agents
        assert set(copies) == {ea.id, dev.id, ux.id, analyst.id}
        assert fork.orchestrator_agent_id == copies[ea.id].id
        rows = {r.agent_id: r for r in await _rows(session, fork.id)}
        assert all(r.role == FLEET_ROLE_MEMBER for r in rows.values())
        assert rows[copies[ux.id].id].parent_member_id == rows[copies[dev.id].id].id
        assert rows[copies[dev.id].id].is_exposed

        ea_copy = copies[ea.id]
        assert ea_copy.handover_agent_ids == [str(copies[dev.id].id), str(copies[ux.id].id)]
        assert ea_copy.handover_types == {str(copies[dev.id].id): "delegate", str(copies[ux.id].id): "consult"}

        copied = (
            (await session.execute(select(Assignment).where(Assignment.agent_id.in_([c.id for c in copies.values()]))))
            .scalars()
            .all()
        )
        # The two set up above, plus the orchestrator's weekly fleet health check (#11700)
        # and the "Fleet health answered" (#1406) and "Overdue work items" (#1662)
        # listeners seeded with it.
        assert len(copied) == result.assignments_copied == 5
        # The tripwire: a fork never produces an enabled assignment, nor one
        # carrying the source's OpenCTI / OpenAEV provisioning state.
        assert not any(a.enabled for a in copied)
        assert all(a.paused_reason == "created_disabled" for a in copied)
        assert all("opencti_provisioning" not in (a.trigger_config or {}) for a in copied)

        # The source is untouched.
        source_rows = await _rows(session, source.id)
        assert {r.agent_id for r in source_rows} == {ea.id, dev.id, ux.id, analyst.id}
        assert (await session.get(Agent, ea.id)).handover_agent_ids == [str(dev.id), str(ux.id)]
        assert await source_changed(session, fork) is False


async def test_a_built_in_fleet_forks_into_a_normal_editable_fleet() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, _agents = await _office(session, alice)
        source.is_builtin = True
        await session.commit()

        result = await fork_fleet(session, source, alice, [])
        await session.commit()

        assert not result.fleet.is_builtin
        assert result.fleet.forked_from["is_builtin"] is True
        assert result.fleet.name == "Copy of Office"
        assert all(not a.is_builtin for a in result.copied_agents.values())


async def test_a_fork_copies_the_documentation_and_a_member_change_marks_it_out_of_date() -> None:
    from app.services.fleet_docs import documentation_view, seed_documentation

    async with _database() as session:
        alice = await _user(session, "alice")
        source, _agents = await _office(session, alice)
        source.is_builtin = True
        await seed_documentation(session, source, {"purpose": "Run the office", "flow": "EA leads"})
        await session.commit()
        assert (await documentation_view(session, source, can_edit=True)).out_of_date is False

        fork = (await fork_fleet(session, source, alice, [])).fleet
        await session.commit()
        view = await documentation_view(session, fork, can_edit=True)
        assert view.sections == {"purpose": "Run the office", "flow": "EA leads"}
        assert view.version == 1 and view.editable and view.out_of_date is False

        fork.default_collaboration_instruction = "Ask the EA first"
        await session.commit()
        assert (await documentation_view(session, fork, can_edit=True)).out_of_date is True
        # The source's documentation does not move with its fork.
        assert (await documentation_view(session, source, can_edit=True)).out_of_date is False


async def test_a_keep_fork_copies_the_orchestrator_and_connects_the_other_members() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, (ea, dev, ux, analyst) = await _office(session, alice)

        result = await fork_fleet(session, source, alice, [], members="keep")
        await session.commit()

        assert set(result.copied_agents) == {ea.id}
        assert set(result.kept_agents) == {dev.id, ux.id, analyst.id}
        rows = await _rows(session, result.fleet.id)
        homes = [r for r in rows if r.role == FLEET_ROLE_MEMBER]
        connected = [r for r in rows if r.role == FLEET_ROLE_CONNECTED]
        assert [r.agent_id for r in homes] == [result.copied_agents[ea.id].id]
        assert {r.agent_id for r in connected} == {dev.id, ux.id, analyst.id}
        # The kept agents stay home in the source fleet.
        assert {r.agent_id for r in await _rows(session, source.id)} >= {dev.id, ux.id, analyst.id}


async def test_a_change_of_the_source_after_the_fork_is_detected() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, (ea, *_rest) = await _office(session, alice)
        result = await fork_fleet(session, source, alice, [])
        await session.commit()
        assert await source_changed(session, result.fleet) is False

        (await session.get(Agent, ea.id)).persona = "You are the EA, now with a new brief."
        await session.commit()
        assert await source_changed(session, result.fleet) is True


async def test_the_source_changes_name_each_object_and_field_that_moved() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, (ea, dev, ux, analyst) = await _office(session, alice)
        result = await fork_fleet(session, source, alice, [])
        await session.commit()
        fork = result.fleet
        # The kept structure carries a persona digest, never the persona.
        kept = fork.forked_from["structure"]
        assert all("You are" not in str(m.get("persona", "")) for m in kept["members"])

        unchanged = await source_changes(session, fork)
        assert unchanged.is_fork and unchanged.source_exists and unchanged.diff_available
        assert unchanged.changed is False and unchanged.changes == []

        (await session.get(Agent, ea.id)).persona = "You are the EA, now with a new brief."
        source.description = "A new purpose."
        for row in await _rows(session, source.id):
            if row.agent_id == analyst.id:
                await session.delete(row)
        await session.commit()
        await session.refresh(source)

        moved = await source_changes(session, fork)
        assert moved.changed is True
        by_name = {(c["object"], c["name"]): c for c in moved.changes}
        assert by_name[("fleet", source.name)]["fields"] == ["description"]
        assert by_name[("member", ea.slug)] == {
            "object": "member",
            "name": ea.slug,
            "action": "changed",
            "fields": ["persona"],
        }
        assert by_name[("member", analyst.slug)]["action"] == "removed"
        assert ("member", dev.slug) not in by_name and ("member", ux.slug) not in by_name


async def test_a_fork_older_than_the_kept_structure_knows_it_changed_but_not_how() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, (ea, *_rest) = await _office(session, alice)
        result = await fork_fleet(session, source, alice, [])
        fork = result.fleet
        fork.forked_from = {k: v for k, v in fork.forked_from.items() if k != "structure"}
        (await session.get(Agent, ea.id)).persona = "You are the EA, now with a new brief."
        await session.commit()

        found = await source_changes(session, fork)
        assert found.changed is True and not found.diff_available and found.changes == []


async def test_a_fleet_that_is_not_a_fork_has_no_source_changes() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        source, _agents = await _office(session, alice)
        found = await source_changes(session, source)
        assert not found.is_fork and found.changed is None and found.changes == []
