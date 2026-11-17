"""``share_fleet_workspace`` on PostgreSQL (#24): the real writers, the real audit rows.

An administrator's agent shares a colleague's fleet and its workspace with a
user, who can then read the fleet through ``get_fleet``; a non-admin who does
not own the workspace is refused and nothing is stored; the unshare removes the
grant on both; every change leaves one audit row per entity naming the agent.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.audit_log import AuditLog
from app.models.fleet import Fleet
from app.models.user import User
from app.models.workspace import Workspace
from app.services.builtin_tools.fleets import build_fleet_tools
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with (
        scratch_database("fleetshare", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
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


async def _call(user: User, tool: str, *, agent_id: str | None = None, **args) -> str:
    _defs, executor = build_fleet_tools(str(user.id), agent_id=agent_id)
    return await executor.handler(tool)(**args)


async def _reload(session, model, entity_id):
    statement = select(model).where(model.id == entity_id).execution_options(populate_existing=True)
    return (await session.execute(statement)).scalar_one()


async def test_admin_shares_a_fleet_and_its_workspace_and_a_stranger_is_refused() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        admin = await _user(session, "root", admin=True)
        bob = await _user(session, "bob")
        mallory = await _user(session, "mallory")
        bob_email, bob_id, admin_id = bob.email, str(bob.id), admin.id
        await session.commit()

        await _call(alice, "create_fleet", name="Ops fleet")
        fleet = (await session.execute(select(Fleet).where(Fleet.name == "Ops fleet"))).scalar_one()
        fleet_id, workspace_id = fleet.id, fleet.workspace_id
        assert "not found or you don't have access" in await _call(bob, "get_fleet", fleet_id=str(fleet_id))

        # A stranger (not admin, not the workspace owner) is refused, nothing stored.
        refused = await _call(mallory, "share_fleet_workspace", fleet_id=str(fleet_id), users=[bob_email])
        assert "not found or you don't have access" in refused or "Only a platform administrator" in refused
        assert (await _reload(session, Workspace, workspace_id)).shared_user_ids is None

        agent_id = str(uuid.uuid4())
        answer = await _call(
            admin, "share_fleet_workspace", agent_id=agent_id, fleet_id=str(fleet_id), users=[bob_email]
        )
        assert "Given access" in answer
        assert (await _reload(session, Fleet, fleet_id)).shared_user_ids == [bob_id]
        assert (await _reload(session, Workspace, workspace_id)).shared_user_ids == [bob_id]
        assert "Ops fleet" in await _call(bob, "get_fleet", fleet_id=str(fleet_id))

        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id.in_([fleet_id, workspace_id]))
                    .where(AuditLog.summary.like("Changed who can open%"))
                )
            )
            .scalars()
            .all()
        )
        assert sorted(row.entity_type for row in rows) == ["fleet", "workspace"]
        for row in rows:
            assert row.user_id == admin_id
            assert row.details["agent_id"] == agent_id
            assert row.details["added_users"] == [bob_id]
            assert row.details["permission"] == "read_only"

        await _call(admin, "share_fleet_workspace", fleet_id=str(fleet_id), action="remove", users=[bob_email])
        assert (await _reload(session, Fleet, fleet_id)).shared_user_ids is None
        assert (await _reload(session, Workspace, workspace_id)).shared_user_ids is None
