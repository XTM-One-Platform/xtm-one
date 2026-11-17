"""AI switched off for a group, on PostgreSQL: the real queries and the real routes.

An admin switches AI off for a group (``PUT /groups/{id}``); a member is then
refused by the AI request gates with the platform switch's own ``ai_disabled``
code, told so by ``/auth/me``, and routed to no LLM provider; a person outside
the group, and the group once switched back on or deleted, are not.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import auth as auth_api
from app.api.v1 import groups as groups_api
from app.core.database import get_db
from app.llm.provider_access import bind_acting_user, load_provider_audience
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services import group_ai_access
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database(
        "groupai", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        group_ai_access.invalidate_cache()
        yield session
        group_ai_access.invalidate_cache()


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(auth_api.router, prefix="/api/v1/auth")
    api.include_router(groups_api.router, prefix="/api/v1/groups")

    # The real gate behind the real authentication step's own binding of the
    # person (`bind_acting_user`): the override binds as the dependency does.
    @api.post("/api/v1/ai", dependencies=[Depends(deps.get_current_user_ungated), Depends(deps.require_ai_enabled)])
    async def _ai():
        return {"ok": True}

    async def _authenticate():
        bind_acting_user(user.id)
        return user

    api.dependency_overrides[deps.get_current_user_ungated] = _authenticate
    api.dependency_overrides[deps.get_current_user] = _authenticate

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


async def _user(session, name: str, *, role: str = "user") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def _group(session, name: str, *members: User) -> Group:
    group = Group(id=uuid.uuid4(), name=f"{name}-{uuid.uuid4().hex[:4]}")
    session.add(group)
    await session.flush()
    for member in members:
        session.add(GroupMember(group_id=group.id, user_id=member.id))
    await session.flush()
    return group


async def _ask(session, user: User) -> httpx.Response:
    async with _client(session, user) as client:
        return await client.post("/api/v1/ai")


async def test_a_member_is_refused_and_a_person_outside_the_group_is_not():
    async with _database() as session:
        admin = await _user(session, "admin", role="admin")
        member = await _user(session, "member")
        outsider = await _user(session, "outsider")
        group = await _group(session, "Legal", member)
        await session.commit()

        assert (await _ask(session, member)).status_code == 200

        async with _client(session, admin) as client:
            off = await client.put(f"/api/v1/groups/{group.id}", json={"ai_enabled": False})
            assert off.status_code == 200, off.text
            assert off.json()["ai_enabled"] is False
            assert (await client.get(f"/api/v1/groups/{group.id}")).json()["ai_enabled"] is False

        refused = await _ask(session, member)
        assert refused.status_code == 403, refused.text
        detail = refused.json()["detail"]
        assert (detail["code"], detail["scope"]) == ("ai_disabled", "group")
        assert (await _ask(session, outsider)).status_code == 200


async def test_the_member_is_told_which_group_and_the_others_are_not():
    async with _database() as session:
        admin = await _user(session, "admin", role="admin")
        member = await _user(session, "member")
        outsider = await _user(session, "outsider")
        zeta = await _group(session, "Zeta", member)
        alpha = await _group(session, "Alpha", member)
        await session.commit()

        async with _client(session, admin) as client:
            for group in (zeta, alpha):
                assert (
                    await client.put(f"/api/v1/groups/{group.id}", json={"ai_enabled": False})
                ).status_code == 200

        async with _client(session, member) as client:
            me = (await client.get("/api/v1/auth/me")).json()
        assert me["ai_disabled_by_group"] is True
        assert me["ai_disabled_group"] == alpha.name

        async with _client(session, outsider) as client:
            me = (await client.get("/api/v1/auth/me")).json()
        assert (me["ai_disabled_by_group"], me["ai_disabled_group"]) == (False, None)


async def test_switching_it_back_on_or_deleting_the_group_gives_ai_back_at_once():
    async with _database() as session:
        admin = await _user(session, "admin", role="admin")
        member = await _user(session, "member")
        group = await _group(session, "Legal", member)
        other = await _group(session, "Ops", member)
        await session.commit()

        async with _client(session, admin) as client:
            for target in (group, other):
                await client.put(f"/api/v1/groups/{target.id}", json={"ai_enabled": False})
            assert (await _ask(session, member)).status_code == 403

            # A rename that does not send the field leaves the switch as it is.
            await client.put(f"/api/v1/groups/{group.id}", json={"name": group.name + "!"})
            assert (await client.get(f"/api/v1/groups/{group.id}")).json()["ai_enabled"] is False

            on = await client.put(f"/api/v1/groups/{group.id}", json={"ai_enabled": True})
            assert on.json()["ai_enabled"] is True
            # Still off through the other group: the most restrictive wins.
            assert (await _ask(session, member)).status_code == 403

            assert (await client.delete(f"/api/v1/groups/{other.id}")).status_code == 204
        assert (await _ask(session, member)).status_code == 200


async def test_only_an_admin_switches_it():
    async with _database() as session:
        member = await _user(session, "member")
        group = await _group(session, "Legal", member)
        await session.commit()
        # The real admin gate, not the override above.
        api = FastAPI()
        api.include_router(groups_api.router, prefix="/api/v1/groups")

        async def _as_member():
            return member

        api.dependency_overrides[deps.get_current_user] = _as_member

        async def _get_db():
            yield session

        api.dependency_overrides[get_db] = _get_db
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            refused = await client.put(f"/api/v1/groups/{group.id}", json={"ai_enabled": False})
        assert refused.status_code == 403
        await session.refresh(group)
        assert group.ai_disabled is False


async def test_the_router_audience_of_a_member_is_ai_disabled_even_without_provider_restrictions():
    async with _database() as session:
        member = await _user(session, "member")
        outsider = await _user(session, "outsider")
        group = await _group(session, "Legal", member)
        group.ai_disabled = True
        await session.commit()
        group_ai_access.invalidate_cache()

        assert (await load_provider_audience(session, member.id, with_groups=False)).ai_disabled is True
        assert (await load_provider_audience(session, outsider.id, with_groups=False)).ai_disabled is False
        assert await group_ai_access.disabling_group_names(session, member.id) == [group.name]
        assert await group_ai_access.member_of_disabled_group(session, outsider.id) is False
