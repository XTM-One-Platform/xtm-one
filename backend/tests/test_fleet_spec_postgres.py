"""Fleet spec export and apply on PostgreSQL, through the real tools.

* **Round trip:** a fleet of one orchestrator and three
  members is exported, deleted with its agents, and re-applied: the new
  export is the same spec.
* **Dry run:** the plan is computed and nothing is written.
* **Idempotent:** applying the exported spec to the live fleet changes nothing.
* **Adoption:** an agent the caller manages under a member key is adopted,
  never edited.
* **Refused:** an agent the caller cannot manage blocks the apply (nothing
  written) unless ``allow_partial``.
* **Company-managed:** the export of a company-managed fleet applied back is
  refused whole, dry run included, never a plan that strips its members.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.services.builtin_tools.fleets import build_fleet_tools
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database(
        "fleetspec", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
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


def _spec_of(answer: str) -> dict:
    match = re.search(r"```json\n(.*)\n```", answer, re.S)
    assert match, answer
    return json.loads(match.group(1))


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _office(session, alice: User) -> tuple[str, list[Agent]]:
    """A fleet of one orchestrator and three members, a hierarchy and handovers."""
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
    return fleet_id, [ea, dev, ux, analyst]


async def test_a_fleet_exported_deleted_and_reapplied_is_identical() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        fleet_id, agents = await _office(session, alice)
        before = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=fleet_id))
        assert before["spec"]["orchestrator"] == agents[0].slug
        assert len(before["spec"]["members"]) == 4
        assert "exported_at" not in json.dumps(before)

        assert "deleted" in (await _call(alice, "delete_fleet", fleet_id=fleet_id)).lower()
        for agent in agents:
            await session.delete(await session.get(Agent, agent.id))
        await session.commit()

        plan = await _call(alice, "apply_fleet_spec", spec=before, dry_run=False)
        assert plan.startswith("Applied."), plan
        assert "Unresolved" not in plan, plan
        new_id = re.search(r"\(`([0-9a-f-]{36})`\)", plan).group(1)  # type: ignore[union-attr]

        after = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=new_id))
        assert after == before


async def test_a_dry_run_writes_nothing_and_a_second_apply_changes_nothing() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        fleet_id, _agents = await _office(session, alice)
        spec = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=fleet_id))

        spec["metadata"]["slug"] = "office-copy"
        for member in spec["spec"]["members"]:
            member["key"] = f"{member['key']}-copy"
            member["parent"] = f"{member['parent']}-copy" if member["parent"] else None
            for handover in member["handovers"]:
                handover["to"] = f"{handover['to']}-copy"
        spec["spec"]["orchestrator"] += "-copy"
        agents_before, fleets_before = await _count(session, Agent), await _count(session, Fleet)
        dry = await _call(alice, "apply_fleet_spec", spec=json.dumps(spec))
        assert dry.startswith("Dry run: nothing was written."), dry
        assert "create fleet" in dry and dry.count("create agent") == 4
        assert (await _count(session, Agent), await _count(session, Fleet)) == (agents_before, fleets_before)

        live = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=fleet_id))
        again = await _call(alice, "apply_fleet_spec", spec=live, dry_run=False)
        assert "create" not in again and "update" not in again and "remove" not in again, again
        assert "unchanged fleet" in again


async def test_an_agent_the_caller_manages_is_adopted_and_never_edited() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        boss = await _agent(session, alice, "Boss")
        await session.commit()
        spec = {
            "apiVersion": "xtm-one/fleet/v1",
            "kind": "Fleet",
            "metadata": {"name": "Solo", "slug": "solo"},
            "spec": {
                "orchestrator": boss.slug,
                "members": [{"key": boss.slug, "agent": {"version": 3, "agent": {"name": "Other", "persona": "x"}}}],
            },
        }
        plan = await _call(alice, "apply_fleet_spec", spec=spec, dry_run=False)
        assert "create agent" not in plan and "add member" in plan, plan
        refreshed = (
            await session.execute(select(Agent).where(Agent.id == boss.id).execution_options(populate_existing=True))
        ).scalar_one()
        assert (refreshed.name, refreshed.persona) == ("Boss", "You are Boss.")


async def test_an_agent_the_caller_cannot_manage_blocks_the_apply() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        theirs = await _agent(session, bob, "Theirs")
        await session.commit()
        spec = {
            "apiVersion": "xtm-one/fleet/v1",
            "kind": "Fleet",
            "metadata": {"name": "Grab", "slug": "grab"},
            "spec": {"members": [{"key": theirs.slug, "agent": {"ref": {"slug": theirs.slug}}}]},
        }
        fleets, members = await _count(session, Fleet), await _count(session, FleetMember)
        refused = await _call(alice, "apply_fleet_spec", spec=spec, dry_run=False)
        assert "cannot manage" in refused and "Nothing was saved." in refused, refused
        assert (await _count(session, Fleet), await _count(session, FleetMember)) == (fleets, members)

        partial = await _call(alice, "apply_fleet_spec", spec=spec, dry_run=False, allow_partial=True)
        assert partial.startswith("Applied.") and "Unresolved" in partial, partial


async def test_the_export_of_a_company_managed_fleet_is_refused_whole() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        fleet_id, _agents = await _office(session, alice)
        exported = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=fleet_id))
        fleet = await session.get(Fleet, uuid.UUID(fleet_id))
        assert fleet is not None
        fleet.is_company_managed = True
        await session.commit()
        members = await _count(session, FleetMember)

        dry = await _call(alice, "apply_fleet_spec", spec=exported)
        assert "company-managed" in dry and "remove" not in dry, dry

        bob = await _user(session, "bob")
        await session.commit()
        theirs = await _call(bob, "apply_fleet_spec", spec=exported)
        assert "company-managed" in theirs and "create" not in theirs, theirs
        assert (await _count(session, Fleet), await _count(session, FleetMember)) == (1, members)
