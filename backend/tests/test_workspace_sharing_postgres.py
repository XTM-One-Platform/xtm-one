"""Sharing a workspace with people and groups, against real PostgreSQL.

The Workspaces page shares a workspace the way every other entity is shared:
per-person and per-group grants with a tier, and the admin-only
company-managed switch whose audience a restriction narrows. What is
pinned here is the part specific to a workspace:

* a ``read_only`` grantee sees the workspace but cannot file a conversation
  into it, a ``config`` grantee can - filing writes into the workspace's
  coordination scope;
* the default workspace is its owner's landing place, so it may be shared but
  never made company-managed, and a company-managed workspace is nobody's
  default;
* a person's grants are pruned with their account.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import chat as chat_api
from app.api.v1 import workspaces as workspaces_api
from app.api.v1.helpers import prune_user_grants
from app.core.database import get_db
from app.models.chat import Conversation
from app.models.group import Group, GroupMember
from app.models.user import User
from app.models.workspace import Workspace
from app.services.workspaces import create_workspace, ensure_default_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database(
        "wsshare", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The workspace and chat routers over one real session, as *user*."""
    api = FastAPI()
    api.include_router(workspaces_api.router, prefix="/api/v1/workspaces")
    api.include_router(chat_api.router, prefix="/api/v1/chat")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None
    api.dependency_overrides[deps.require_chat_enabled] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


async def _user(session, name: str, *, admin: bool = False, active: bool = True) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
        is_active=active,
    )
    session.add(user)
    await session.flush()
    return user


async def _conversation(session, owner: User) -> Conversation:
    conversation = Conversation(id=uuid.uuid4(), user_id=owner.id, channel="web", title="t")
    session.add(conversation)
    await session.flush()
    return conversation


def _ids(listing: httpx.Response) -> set[str]:
    assert listing.status_code == 200, listing.text
    return {row["id"] for row in listing.json()}


async def test_a_read_only_grantee_sees_it_and_a_config_grantee_files_into_it() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        workspace = await create_workspace(session, user_id=alice.id, name="Acme audit")
        bobs = await _conversation(session, bob)
        await session.commit()
        url = f"/api/v1/workspaces/{workspace.id}"

        async with _client(session, alice) as client:
            shared = await client.put(
                url,
                json={
                    "shared_user_ids": [str(bob.id)],
                    "shared_user_permissions": {str(bob.id): "read_only"},
                },
            )
            assert shared.status_code == 200, shared.text
            assert shared.json()["shared_user_ids"] == [str(bob.id)]
            assert shared.json()["shared_user_permissions"] == {str(bob.id): "read_only"}

        async with _client(session, bob) as client:
            listing = await client.get("/api/v1/workspaces")
            assert str(workspace.id) in _ids(listing)
            row = next(r for r in listing.json() if r["id"] == str(workspace.id))
            assert row["can_manage"] is False
            refused = await client.patch(
                f"/api/v1/chat/conversations/{bobs.id}", json={"workspace_id": str(workspace.id)}
            )
            assert refused.status_code == 403, refused.text

        async with _client(session, alice) as client:
            upgraded = await client.put(
                url, json={"shared_user_permissions": {str(bob.id): "config"}}
            )
            assert upgraded.status_code == 200, upgraded.text

        async with _client(session, bob) as client:
            filed = await client.patch(
                f"/api/v1/chat/conversations/{bobs.id}", json={"workspace_id": str(workspace.id)}
            )
            assert filed.status_code == 200, filed.text
            assert filed.json()["workspace_id"] == str(workspace.id)

        async with _client(session, alice) as client:
            unshared = await client.put(
                url, json={"shared_user_ids": None, "shared_user_permissions": None}
            )
            assert unshared.status_code == 200, unshared.text
            assert unshared.json()["shared_user_ids"] is None

        async with _client(session, bob) as client:
            assert str(workspace.id) not in _ids(await client.get("/api/v1/workspaces"))
        # Unshared, Bob's conversation stays filed there, as the docs say.
        await session.refresh(bobs)
        assert bobs.workspace_id == workspace.id


async def test_sharing_with_an_unknown_or_inactive_person_is_refused() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        gone = await _user(session, "gone", active=False)
        workspace = await create_workspace(session, user_id=alice.id, name="W")
        await session.commit()
        async with _client(session, alice) as client:
            for grantee in (str(gone.id), str(uuid.uuid4())):
                refused = await client.put(
                    f"/api/v1/workspaces/{workspace.id}", json={"shared_user_ids": [grantee]}
                )
                assert refused.status_code == 422, refused.text
        await session.refresh(workspace)
        assert workspace.shared_user_ids is None


async def test_the_default_can_be_shared_but_never_company_managed() -> None:
    async with _database() as session:
        admin = await _user(session, "admin", admin=True)
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        default = await ensure_default_workspace(session, alice.id)
        await session.commit()
        url = f"/api/v1/workspaces/{default.id}"

        async with _client(session, admin) as client:
            refused = await client.put(url, json={"is_company_managed": True})
            assert refused.status_code == 400, refused.text
            assert "cannot be company-managed" in refused.json()["detail"]
        await session.refresh(default)
        assert default.is_company_managed is False and default.is_default is True

        async with _client(session, alice) as client:
            shared = await client.put(
                url,
                json={
                    "shared_user_ids": [str(bob.id)],
                    "shared_user_permissions": {str(bob.id): "read_only"},
                },
            )
            assert shared.status_code == 200, shared.text
        async with _client(session, bob) as client:
            assert str(default.id) in _ids(await client.get("/api/v1/workspaces"))


async def test_a_company_managed_workspace_restricted_to_people_is_nobodys_default() -> None:
    async with _database() as session:
        admin = await _user(session, "admin", admin=True)
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        carol = await _user(session, "carol")
        await ensure_default_workspace(session, alice.id)
        workspace = await create_workspace(session, user_id=alice.id, name="Team")
        await session.commit()
        url = f"/api/v1/workspaces/{workspace.id}"

        async with _client(session, admin) as client:
            published = await client.put(
                url,
                json={
                    "is_company_managed": True,
                    "access_restricted": True,
                    "shared_user_ids": [str(bob.id)],
                    "shared_user_permissions": {str(bob.id): "read_only"},
                },
            )
            assert published.status_code == 200, published.text
            body = published.json()
            assert body["is_company_managed"] is True
            assert body["access_restricted"] is True
            # The restriction keeps the grants: they ARE the audience now.
            assert body["shared_user_ids"] == [str(bob.id)]

        async with _client(session, bob) as client:
            assert str(workspace.id) in _ids(await client.get("/api/v1/workspaces"))
        async with _client(session, carol) as client:
            assert str(workspace.id) not in _ids(await client.get("/api/v1/workspaces"))

        async with _client(session, alice) as client:
            refused = await client.post(f"{url}/default")
            # Company-managed is admins-only to change, the owner included.
            assert refused.status_code == 403, refused.text
        # The one who could otherwise make it their default: an admin owning a
        # company-managed workspace.
        await ensure_default_workspace(session, admin.id)
        own = await create_workspace(session, user_id=admin.id, name="Admin team")
        await session.commit()
        async with _client(session, admin) as client:
            made = await client.put(f"/api/v1/workspaces/{own.id}", json={"is_company_managed": True})
            assert made.status_code == 200, made.text
            refused = await client.post(f"/api/v1/workspaces/{own.id}/default")
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"] == "A company-managed workspace cannot be the default."

        async with _client(session, admin) as client:
            opened = await client.put(url, json={"access_restricted": False})
            assert opened.status_code == 200, opened.text
            # Unrestricted company-managed: every user sees it, grants are dropped.
            assert opened.json()["shared_user_ids"] is None
        async with _client(session, carol) as client:
            assert str(workspace.id) in _ids(await client.get("/api/v1/workspaces"))


async def test_a_deleted_persons_workspace_grants_are_pruned() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        workspace = await create_workspace(session, user_id=alice.id, name="W")
        workspace.shared_user_ids = [str(bob.id)]
        workspace.shared_user_permissions = {str(bob.id): "config"}
        await session.commit()

        assert await prune_user_grants(session, bob.id) == 1
        await session.commit()
        reloaded = await session.get(Workspace, workspace.id)
        await session.refresh(reloaded)
        assert reloaded.shared_user_ids is None
        assert reloaded.shared_user_permissions is None


async def test_a_group_grant_reaches_its_members_only() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        carol = await _user(session, "carol")
        group = Group(id=uuid.uuid4(), name=f"Auditors-{uuid.uuid4().hex[:4]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=bob.id))
        workspace = await create_workspace(session, user_id=alice.id, name="Acme audit")
        bobs = await _conversation(session, bob)
        await session.commit()

        async with _client(session, alice) as client:
            shared = await client.put(
                f"/api/v1/workspaces/{workspace.id}",
                json={
                    "shared_group_ids": [str(group.id)],
                    "shared_group_permissions": {str(group.id): "config"},
                },
            )
            assert shared.status_code == 200, shared.text

        async with _client(session, bob) as client:
            filed = await client.patch(
                f"/api/v1/chat/conversations/{bobs.id}", json={"workspace_id": str(workspace.id)}
            )
            assert filed.status_code == 200, filed.text
        async with _client(session, carol) as client:
            assert str(workspace.id) not in _ids(await client.get("/api/v1/workspaces"))


async def test_only_an_admin_makes_a_workspace_company_managed_even_a_default() -> None:
    """A non-admin is refused as a non-admin (403), before the default rule (400)."""
    async with _database() as session:
        alice = await _user(session, "alice")
        default = await ensure_default_workspace(session, alice.id)
        other = await create_workspace(session, user_id=alice.id, name="Other")
        await session.commit()
        async with _client(session, alice) as client:
            for workspace in (default, other):
                refused = await client.put(
                    f"/api/v1/workspaces/{workspace.id}", json={"is_company_managed": True}
                )
                assert refused.status_code == 403, (workspace.name, refused.text)
        for workspace in (default, other):
            await session.refresh(workspace)
            assert workspace.is_company_managed is False


async def test_a_config_grantee_may_share_on_and_delete_the_workspace() -> None:
    """`Can configure` is the manage right of every entity: it re-shares and
    deletes. The owner is never removed by it."""
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        carol = await _user(session, "carol")
        workspace = await create_workspace(session, user_id=alice.id, name="Acme audit")
        workspace.shared_user_ids = [str(bob.id)]
        workspace.shared_user_permissions = {str(bob.id): "config"}
        await session.commit()
        url = f"/api/v1/workspaces/{workspace.id}"

        async with _client(session, bob) as client:
            reshared = await client.put(
                url,
                json={
                    "shared_user_ids": [str(bob.id), str(carol.id)],
                    "shared_user_permissions": {str(bob.id): "config", str(carol.id): "read_only"},
                },
            )
            assert reshared.status_code == 200, reshared.text
            assert reshared.json()["user_id"] == str(alice.id), "the owner stays the owner"
            takeover = await client.put(url, json={"user_id": str(bob.id)})
            assert takeover.status_code == 403, takeover.text

        async with _client(session, carol) as client:
            assert str(workspace.id) in _ids(await client.get("/api/v1/workspaces"))

        async with _client(session, bob) as client:
            deleted = await client.delete(url)
            assert deleted.status_code == 204, deleted.text
        assert await session.get(Workspace, workspace.id) is None
