"""An imported assignment's MCP scope only names servers its importer can see.

The assignment engine loads every id of ``tool_sources`` with no visibility
check (``load_mcp_tools`` filters on ``enabled`` alone), so the import keeps an
id carried by the file only when the shared three-tier predicate says the
importer can see that server. Admins bypass it, as they do when they add a
server to an assignment by hand: an administrator's import keeps every
server that exists here, and still drops an id that names none. The predicate
reads the per-user sharing columns, which a mock session cannot show: the real
import runs here against PostgreSQL, as a NON-ADMIN importer and as an ADMIN
one. Same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agent_import
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_NO_SUCH_SERVER = str(uuid.uuid4())


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "toolsrc",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


async def _seed(sessions) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
    """An importer (in a group), an admin, and a server of every visibility tier."""
    async with sessions() as session:
        owner, importer, admin = _user("owner"), _user("importer"), _user("admin", role="admin")
        session.add_all([owner, importer, admin])
        await session.flush()
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=importer.id))

        def _server(name: str, **visibility) -> MCPServer:
            server = MCPServer(
                name=name,
                transport="streamable_http",
                url="https://example.invalid/mcp",
                **visibility,
            )
            session.add(server)
            return server

        servers = {
            "own": _server("Own", user_id=importer.id),
            "company": _server("Company", user_id=None, is_company_managed=True),
            "group": _server(
                "Group shared", user_id=owner.id, shared_group_ids=[str(group.id)]
            ),
            "user": _server(
                "User shared", user_id=owner.id, shared_user_ids=[str(importer.id)]
            ),
            "private": _server("Private", user_id=owner.id),
            "restricted": _server(
                "Restricted",
                user_id=None,
                is_company_managed=True,
                access_restricted=True,
            ),
            # Visibility is the whole rule: the importer's own server stays in
            # scope whether it is hidden from chat or switched off, exactly as
            # the assignment API lets them scope it (the loader skips a
            # disabled one at run time).
            "hidden": _server("Hidden", user_id=importer.id, hide_from_chat=True),
            "disabled": _server("Disabled", user_id=importer.id, enabled=False),
        }
        await session.commit()
        return importer.id, admin.id, {key: str(s.id) for key, s in servers.items()}


def _bundle(ids: dict[str, str]) -> dict:
    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Scoped"},
        "mcp_servers": [
            {
                "ref": "mcp-0",
                "name": "Docs",
                "transport": "streamable_http",
                "url": "https://example.invalid/mcp",
            }
        ],
        "assignments": [
            {
                "name": "Mixed",
                "prompt": "Work",
                "tool_sources": [
                    "mcp-0",
                    ids["own"],
                    ids["company"],
                    ids["group"],
                    ids["user"],
                    ids["hidden"],
                    ids["disabled"],
                    ids["private"],
                    ids["restricted"],
                    _NO_SUCH_SERVER,
                ],
            },
            {"name": "Foreign only", "prompt": "Work", "tool_sources": [ids["private"]]},
        ],
    }


async def _import_as(sessions, user_id: uuid.UUID, bundle: dict):
    async with sessions() as session:
        user = await session.get(User, user_id)
        imported = await agent_import.create_agent_from_bundle(
            session, user=user, bundle=bundle
        )
        await session.commit()
    async with sessions() as session:
        rows = await session.execute(
            select(Assignment).where(Assignment.agent_id == imported.agent.id)
        )
        scopes = {wf.name: wf.tool_sources for wf in rows.scalars().all()}
    return imported, scopes


@pytest.mark.asyncio
async def test_a_non_admin_import_keeps_only_the_servers_the_importer_can_see():
    async with _temp_database() as sessions:
        importer_id, _admin_id, ids = await _seed(sessions)
        imported, scopes = await _import_as(sessions, importer_id, _bundle(ids))

    [recreated] = imported.agent.mcp_server_ids
    assert scopes["Mixed"] == [
        recreated,
        ids["own"],
        ids["company"],
        ids["group"],
        ids["user"],
        ids["hidden"],
        ids["disabled"],
    ]
    assert scopes["Foreign only"] == [agent_import.UNRESOLVED_TOOL_SOURCE]
    assert len(imported.warnings) == 2


@pytest.mark.asyncio
async def test_an_admin_import_keeps_every_server_that_exists_here():
    """An admin adds any server to an assignment by hand, so an admin's
    import keeps a colleague's private server too; the id naming no server is
    still dropped."""
    async with _temp_database() as sessions:
        _importer_id, admin_id, ids = await _seed(sessions)
        imported, scopes = await _import_as(sessions, admin_id, _bundle(ids))

    [recreated] = imported.agent.mcp_server_ids
    assert scopes["Mixed"] == [
        recreated,
        ids["own"],
        ids["company"],
        ids["group"],
        ids["user"],
        ids["hidden"],
        ids["disabled"],
        ids["private"],
        ids["restricted"],
    ]
    assert scopes["Foreign only"] == [ids["private"]]
    assert len(imported.warnings) == 1
    assert "'Mixed'" in imported.warnings[0]
