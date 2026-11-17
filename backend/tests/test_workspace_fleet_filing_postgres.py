"""A fleet's work belongs to the workspace hosting it, on PostgreSQL.

What is pinned:

* **Filing by default**: a work item created by a home member of a fleet with
  no workspace named lands in the workspace hosting the fleet, when the caller
  may write there; a named workspace wins; a caller without the write tier
  and an archived fleet leave it unfiled, as before.
* **Counters**: ``load_workspace_contents`` counts what the hosted fleet's
  home members produced elsewhere for a caller who can see it, once per item;
  ``held_work_items`` stays the items filed in the workspace (the delete
  refusal); without a caller only what is filed is counted.
* **Backfill**: ``w7245filefleet1`` files the unfiled items of home members
  owned by the workspace owner, leaves another user's item alone, and a
  second run files nothing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.user import User
from app.models.work_item import WorkItem
from app.models.workspace import Workspace
from app.services.company_managed_writes import person_writer
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_PERSON = person_writer(is_admin=False)
_REVISION = next(
    (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("w7245filefleet1_*.py")
)


@asynccontextmanager
async def _database():
    async with scratch_database(
        "wsfleetfile", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db


@asynccontextmanager
async def _service_sessions(db):
    """Point ``async_session`` at the scratch database: the work-item service
    opens its own session per operation."""
    from app.core import database as db_module

    original = db_module.async_session
    db_module.async_session = db.sessions
    try:
        yield
    finally:
        db_module.async_session = original


async def _seed(session, *, archived_fleet: bool = False):
    alice = User(id=uuid.uuid4(), email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
                 full_name="alice", hashed_password="x", role="user")
    bob = User(id=uuid.uuid4(), email=f"bob-{uuid.uuid4().hex[:6]}@example.com",
               full_name="bob", hashed_password="x", role="user")
    session.add_all([alice, bob])
    await session.flush()
    hosting = Workspace(id=uuid.uuid4(), user_id=alice.id, name="CTEM", slug="ctem")
    other = Workspace(id=uuid.uuid4(), user_id=alice.id, name="Other", slug="other")
    session.add_all([hosting, other])
    await session.flush()
    member = Agent(id=uuid.uuid4(), user_id=alice.id, name="Triage", slug=f"triage-{uuid.uuid4().hex[:6]}")
    outsider = Agent(id=uuid.uuid4(), user_id=alice.id, name="Solo", slug=f"solo-{uuid.uuid4().hex[:6]}")
    session.add_all([member, outsider])
    await session.flush()
    fleet = Fleet(id=uuid.uuid4(), user_id=alice.id, workspace_id=hosting.id, name="CTEM Team",
                  slug="ctem-team", is_archived=archived_fleet)
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=member.id, role=FLEET_ROLE_MEMBER))
    await session.commit()
    return alice, bob, hosting, other, member, outsider


async def test_a_fleet_members_new_item_is_filed_in_the_hosting_workspace() -> None:
    from app.services.work_item import create_work_item

    async with _database() as db:
        async with db.sessions() as session:
            alice, bob, hosting, other, member, outsider = await _seed(session)
        async with _service_sessions(db):
            filed = await create_work_item(
                user_id=str(alice.id), title="From the fleet", created_by_agent_id=str(member.id),
                writer=_PERSON,
            )
            named = await create_work_item(
                user_id=str(alice.id), title="Named", created_by_agent_id=str(member.id),
                workspace_id=other.id, writer=_PERSON,
            )
            solo = await create_work_item(
                user_id=str(alice.id), title="Solo", created_by_agent_id=str(outsider.id),
                writer=_PERSON,
            )
            # Bob cannot write in Alice's workspace: his item stays unfiled.
            bobs = await create_work_item(
                user_id=str(bob.id), title="Bob's", created_by_agent_id=str(member.id),
                writer=_PERSON,
            )
        async with db.sessions() as session:
            rows = {
                r.title: r.workspace_id
                for r in (await session.execute(select(WorkItem))).scalars().all()
            }
        assert rows["From the fleet"] == hosting.id
        assert rows["Named"] == other.id
        assert rows["Solo"] is None
        assert rows["Bob's"] is None
        assert {filed["title"], named["title"], solo["title"], bobs["title"]} == set(rows)


async def test_an_archived_fleet_files_nothing() -> None:
    from app.services.work_item import create_work_item

    async with _database() as db:
        async with db.sessions() as session:
            alice, _bob, _hosting, _other, member, _outsider = await _seed(session, archived_fleet=True)
        async with _service_sessions(db):
            await create_work_item(
                user_id=str(alice.id), title="After the archive", created_by_agent_id=str(member.id),
                writer=_PERSON,
            )
        async with db.sessions() as session:
            row = (await session.execute(select(WorkItem))).scalars().one()
        assert row.workspace_id is None


async def test_the_holds_line_counts_the_fleets_work_once_and_keeps_the_delete_count() -> None:
    from app.services.workspaces import load_workspace_contents

    async with _database() as db:
        async with db.sessions() as session:
            alice, bob, hosting, other, member, outsider = await _seed(session)
            session.add_all([
                # Filed in the hosting workspace.
                WorkItem(user_id=alice.id, workspace_id=hosting.id, title="filed", status="open"),
                # Unfiled, owned and created by the member: counted once.
                WorkItem(user_id=alice.id, owner_agent_id=member.id, created_by_agent_id=member.id,
                         title="unfiled", status="open"),
                # Filed elsewhere, created by the member: counted, done.
                WorkItem(user_id=alice.id, workspace_id=other.id, created_by_agent_id=member.id,
                         title="elsewhere", status="done"),
                # Another agent's: not counted.
                WorkItem(user_id=alice.id, created_by_agent_id=outsider.id, title="solo", status="open"),
                # Bob's private item of the member: not visible to Alice.
                WorkItem(user_id=bob.id, created_by_agent_id=member.id, title="bob", status="open"),
            ])
            await session.commit()
            hosting_row = await session.get(Workspace, hosting.id)
            counts = (await load_workspace_contents(session, [hosting_row], user_id=alice.id))[str(hosting.id)]
            filed_only = (await load_workspace_contents(session, [hosting_row]))[str(hosting.id)]
        assert counts["work_items"] == 3
        assert counts["open_work_items"] == 2
        assert counts["held_work_items"] == 1
        assert filed_only["work_items"] == 1
        assert filed_only["held_work_items"] == 1


async def test_the_backfill_files_the_owners_items_once() -> None:
    spec = importlib.util.spec_from_file_location(_REVISION.stem, _REVISION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with _database() as db:
        async with db.sessions() as session:
            alice, bob, hosting, _other, member, outsider = await _seed(session)
            session.add_all([
                WorkItem(user_id=alice.id, owner_agent_id=member.id, title="owned", status="open"),
                WorkItem(user_id=alice.id, created_by_agent_id=member.id, title="created", status="done"),
                WorkItem(user_id=alice.id, created_by_agent_id=outsider.id, title="solo", status="open"),
                WorkItem(user_id=bob.id, created_by_agent_id=member.id, title="bob", status="open"),
            ])
            await session.commit()
            conn = await session.connection()
            first = await conn.run_sync(lambda sync: migration.file_in_batches(sync, batch_size=1))
            second = await conn.run_sync(lambda sync: migration.file_in_batches(sync))
            await session.commit()
            rows = {
                r.title: r.workspace_id
                for r in (await session.execute(select(WorkItem))).scalars().all()
            }
        assert first == 2
        assert second == 0
        assert rows == {"owned": hosting.id, "created": hosting.id, "solo": None, "bob": None}
