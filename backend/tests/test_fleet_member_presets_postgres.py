"""Fleet member presets on PostgreSQL, through the API (#9162 R4).

* The list answers the three built-ins first, then the company's by name.
* An administrator creates, renames, changes and deletes a preset; a
  non-administrator reads only (403 on every write).
* A name is unique without regard to case; a built-in answers 403.
* A ``list`` scope naming no active organization is refused, nothing stored.
* Deleting a preset changes no fleet: applying a preset copies its values.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import fleet_member_presets as presets_api
from app.core.database import get_db
from app.models.fleet import FleetMemberPreset
from app.models.user import User
from app.services.fleet_member_presets import BUILTIN_PRESETS
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

BASE = "/api/v1/fleet-member-presets"


@asynccontextmanager
async def _database():
    async with (
        scratch_database(
            "fleet_presets", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as db,
        db.sessions() as session,
    ):
        yield session


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(presets_api.router, prefix=BASE)
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _user(session, name: str, *, admin: bool) -> User:
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


async def test_an_admin_manages_presets_and_a_user_reads_them():
    async with _database() as session:
        admin = await _user(session, "admin", admin=True)
        member = await _user(session, "member", admin=False)
        async with _client(session, admin) as client:
            created = await client.post(
                BASE, json={"name": "Legal desk", "values": {"collaboration_instruction": "Contracts only"}}
            )
            assert created.status_code == 201, created.text
            preset = created.json()
            assert preset["builtin"] is False
            assert preset["values"] == {"collaboration_instruction": "Contracts only"}

            dup = await client.post(BASE, json={"name": "legal DESK", "values": {"context_inclusion": "none"}})
            assert dup.status_code == 409

            renamed = await client.patch(
                f"{BASE}/{preset['id']}", json={"name": "Contracts", "values": {"context_inclusion": "full"}}
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["name"] == "Contracts"
            assert renamed.json()["values"] == {"context_inclusion": "full"}

            builtin = BUILTIN_PRESETS[0].id
            assert (await client.patch(f"{BASE}/{builtin}", json={"name": "x"})).status_code == 403
            assert (await client.delete(f"{BASE}/{builtin}")).status_code == 403

        async with _client(session, member) as client:
            listed = (await client.get(BASE)).json()
            assert [p["id"] for p in listed[:3]] == [p.id for p in BUILTIN_PRESETS]
            assert [p["name"] for p in listed[3:]] == ["Contracts"]
            assert (
                await client.post(BASE, json={"name": "Mine", "values": {"context_inclusion": "none"}})
            ).status_code == 403
            assert (await client.delete(f"{BASE}/{preset['id']}")).status_code == 403

        async with _client(session, admin) as client:
            assert (await client.delete(f"{BASE}/{preset['id']}")).status_code == 204
            assert (await client.delete(f"{BASE}/{preset['id']}")).status_code == 404
        assert (await session.execute(select(FleetMemberPreset))).scalars().all() == []


async def test_a_scope_naming_no_active_organization_is_refused():
    async with _database() as session:
        admin = await _user(session, "admin", admin=True)
        async with _client(session, admin) as client:
            refused = await client.post(
                BASE,
                json={
                    "name": "Ghost",
                    "values": {"organization_scope": {"mode": "list", "organization_ids": [str(uuid.uuid4())]}},
                },
            )
            assert refused.status_code == 422
            assert "no active organization" in refused.json()["detail"]
        assert (await session.execute(select(FleetMemberPreset))).scalars().all() == []
