"""SCIM 2.0 provisioning through the real routers against real PostgreSQL (#122).

Replays the request shapes Microsoft Entra sends: the discovery documents, a
user created, found by ``userName`` and ``externalId``, an SSO account linked
rather than duplicated, ``active`` switched off by a PATCH ``replace`` with no
path and a string boolean (the deactivation cascade runs and revokes the
account's API key), switched back on, then deprovisioned by DELETE; groups
created, their members added and removed, an administrator's membership kept.
The SCIM token alone opens the surface, a revoked one is refused, every write is
audited.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api import deps
from app.api.v1 import scim
from app.core.database import get_db
from app.models.api_key import UserApiKey
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.user import User
from app.models.user_deactivation import UserDeactivation
from app.services import scim as s
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_PLATFORM_ADMIN = "platform-admin@test.local"
_SCIM = "/api/v1/scim/v2"


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", _PLATFORM_ADMIN)
    monkeypatch.setattr("app.services.notify.notify_web_user", AsyncMock())
    # No Redis in the test: the rate limit and the cache drops are not under test.
    monkeypatch.setattr(scim, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("app.services.quota.invalidate_membership_quota_cache", AsyncMock())
    monkeypatch.setattr("app.services.group_ai_access.invalidate_cache_after_commit", AsyncMock())
    # The cascade's post-commit step opens its own sessions on the app engine.
    monkeypatch.setattr("app.services.user_deactivation.after_commit", AsyncMock())


@asynccontextmanager
async def _client(
    sessions: async_sessionmaker[AsyncSession], admin: User, scim_db: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(scim.scim_router, prefix=_SCIM)
    app.include_router(scim.tokens_router, prefix="/api/v1/scim/tokens")

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    # The token's last-used stamp opens its own session on the app's engine.
    scim_db.setattr("app.core.database.async_session", sessions)
    app.dependency_overrides[deps.get_current_user] = lambda: admin
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://scim.test") as client:
        yield client


def _user(name: str, *, role: str = "user", email: str | None = None, days: int = 0) -> User:
    return User(
        email=email or f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
        created_at=datetime.now(UTC) - timedelta(days=days),
    )


async def test_entra_user_and_group_provisioning_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    async with scratch_database("scim", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db:
        async with db.sessions() as session:
            admin = _user("admin", role="admin", email=_PLATFORM_ADMIN, days=30)
            sso_user = _user("sso", email="sso.person@example.com")
            session.add_all([admin, sso_user])
            await session.commit()
            admin_id, sso_id = admin.id, sso_user.id

        async with _client(db.sessions, admin, monkeypatch) as client:
            # The token: generated once, refused without it.
            created = await client.post("/api/v1/scim/tokens", json={"name": "Entra"})
            assert created.status_code == 201, created.text
            raw = created.json()["token"]
            assert raw.startswith("scim-")
            assert created.json()["endpoint"] == f"https://scim.test{_SCIM}"
            listing = (await client.get("/api/v1/scim/tokens")).json()
            assert listing["tokens"][0]["name"] == "Entra" and "token" not in listing["tokens"][0]
            assert listing["tokens"][0]["last_used_at"] is None

            refused = await client.get(f"{_SCIM}/Users")
            assert refused.status_code == 401
            assert refused.json()["schemas"] == [s.ERROR_SCHEMA]
            assert refused.headers["content-type"].startswith(s.SCIM_CONTENT_TYPE)
            wrong = await client.get(f"{_SCIM}/Users", headers={"Authorization": "Bearer scim-nope"})
            assert wrong.status_code == 401

            h = {"Authorization": f"Bearer {raw}"}

            # Discovery.
            spc = await client.get(f"{_SCIM}/ServiceProviderConfig", headers=h)
            assert spc.status_code == 200 and spc.json()["patch"]["supported"] is True
            assert (await client.get(f"{_SCIM}/ResourceTypes", headers=h)).json()["totalResults"] == 2
            assert (await client.get(f"{_SCIM}/Schemas", headers=h)).json()["totalResults"] == 3

            # Entra's probe: a userName nobody has.
            probe = await client.get(
                f"{_SCIM}/Users", params={"filter": 'userName eq "new.person@example.com"'}, headers=h
            )
            assert probe.json()["totalResults"] == 0 and probe.json()["Resources"] == []

            # Create.
            body = {
                "schemas": [s.USER_SCHEMA, s.ENTERPRISE_SCHEMA],
                "externalId": "ext-new",
                "userName": "New.Person@example.com",
                "active": True,
                "displayName": "New Person",
                "emails": [{"primary": True, "type": "work", "value": "New.Person@example.com"}],
                s.ENTERPRISE_SCHEMA: {"department": "Security"},
            }
            made = await client.post(f"{_SCIM}/Users", json=body, headers=h)
            assert made.status_code == 201, made.text
            new_id = made.json()["id"]
            assert made.json()["userName"] == "new.person@example.com"
            assert made.json()[s.ENTERPRISE_SCHEMA]["department"] == "Security"
            by_ext = await client.get(f"{_SCIM}/Users", params={"filter": 'externalId eq "ext-new"'}, headers=h)
            assert [r["id"] for r in by_ext.json()["Resources"]] == [new_id]

            # An SSO account is linked, not duplicated.
            linked = await client.post(
                f"{_SCIM}/Users",
                json={"userName": "SSO.Person@example.com", "externalId": "ext-sso", "active": True},
                headers=h,
            )
            assert linked.status_code == 201 and linked.json()["id"] == str(sso_id)
            assert linked.json()["externalId"] == "ext-sso"

            # A personal key, so the cascade has something to revoke.
            async with db.sessions() as session:
                session.add(
                    UserApiKey(user_id=uuid.UUID(new_id), name="k", key_prefix="fcp-t", key_hash="h", is_active=True)
                )
                await session.commit()

            # Deactivate as Entra does it.
            off = await client.patch(
                f"{_SCIM}/Users/{new_id}",
                json={"schemas": [s.PATCH_SCHEMA], "Operations": [{"op": "Replace", "value": {"active": "False"}}]},
                headers=h,
            )
            assert off.status_code == 200, off.text
            assert off.json()["active"] is False
            async with db.sessions() as session:
                user = await session.get(User, uuid.UUID(new_id))
                assert user.is_active is False and user.deactivation_source == "scim"
                key = (await session.execute(select(UserApiKey).where(UserApiKey.user_id == user.id))).scalar_one()
                assert key.revoked_at is not None
                episode = (
                    await session.execute(select(UserDeactivation).where(UserDeactivation.user_id == user.id))
                ).scalar_one()
                assert episode.actor_label == "SCIM (Entra)" and episode.reactivated_at is None

            # Idempotent, then back on.
            again = await client.patch(
                f"{_SCIM}/Users/{new_id}",
                json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
                headers=h,
            )
            assert again.status_code == 200
            on = await client.patch(
                f"{_SCIM}/Users/{new_id}",
                json={"Operations": [{"op": "replace", "path": "active", "value": "True"}]},
                headers=h,
            )
            assert on.json()["active"] is True

            # Groups: create with a member, add, remove; an administrator's membership stays.
            group = await client.post(
                f"{_SCIM}/Groups",
                json={
                    "schemas": [s.GROUP_SCHEMA],
                    "displayName": "SOC",
                    "externalId": "g-ext",
                    "members": [{"value": new_id}],
                },
                headers=h,
            )
            assert group.status_code == 201, group.text
            gid = group.json()["id"]
            assert [m["value"] for m in group.json()["members"]] == [new_id]
            async with db.sessions() as session:
                session.add(GroupMember(group_id=uuid.UUID(gid), user_id=admin_id, source="manual"))
                await session.commit()
            added = await client.patch(
                f"{_SCIM}/Groups/{gid}",
                json={"Operations": [{"op": "Add", "path": "members", "value": [{"value": str(sso_id)}]}]},
                headers=h,
            )
            assert {m["value"] for m in added.json()["members"]} == {new_id, str(sso_id), str(admin_id)}
            removed = await client.patch(
                f"{_SCIM}/Groups/{gid}",
                json={
                    "Operations": [
                        {"op": "Remove", "path": f'members[value eq "{new_id}"]'},
                        {"op": "Remove", "path": f'members[value eq "{admin_id}"]'},
                    ]
                },
                headers=h,
            )
            assert {m["value"] for m in removed.json()["members"]} == {str(sso_id), str(admin_id)}
            member_of = await client.get(
                f"{_SCIM}/Groups", params={"filter": f'members[value eq "{sso_id}"]'}, headers=h
            )
            assert [g["id"] for g in member_of.json()["Resources"]] == [gid]
            unknown = await client.patch(
                f"{_SCIM}/Groups/{gid}",
                json={"Operations": [{"op": "add", "path": "members", "value": [{"value": str(uuid.uuid4())}]}]},
                headers=h,
            )
            assert unknown.status_code == 400 and unknown.json()["scimType"] == "invalidValue"

            # Deprovision: deactivated and stamped, nothing purged.
            gone = await client.delete(f"{_SCIM}/Users/{new_id}", headers=h)
            assert gone.status_code == 204
            async with db.sessions() as session:
                user = await session.get(User, uuid.UUID(new_id))
                assert user is not None and user.is_active is False and user.deprovisioned_at is not None
            assert (await client.get(f"{_SCIM}/Users/{new_id}", headers=h)).json()["active"] is False
            assert (await client.get(f"{_SCIM}/Users/{uuid.uuid4()}", headers=h)).status_code == 404

            assert (await client.delete(f"{_SCIM}/Groups/{gid}", headers=h)).status_code == 204
            async with db.sessions() as session:
                assert await session.get(Group, uuid.UUID(gid)) is None

            # Every write is audited as the token.
            async with db.sessions() as session:
                rows = (
                    (await session.execute(select(AuditLog).where(AuditLog.user_email == "system:scim:Entra")))
                    .scalars()
                    .all()
                )
                actions = {(r.entity_type, r.action) for r in rows}
                assert {
                    ("user", "create"),
                    ("user", "toggle"),
                    ("user", "delete"),
                    ("group", "create"),
                    ("group", "delete"),
                } <= actions

            # A revoked token is refused at once; its last use was stamped.
            token_id = listing["tokens"][0]["id"]
            assert (await client.get("/api/v1/scim/tokens")).json()["tokens"][0]["last_used_at"] is not None
            assert (await client.delete(f"/api/v1/scim/tokens/{token_id}")).status_code == 204
            assert (await client.get(f"{_SCIM}/Users", headers=h)).status_code == 401


async def test_the_platform_admin_cannot_be_deactivated_by_scim(monkeypatch: pytest.MonkeyPatch) -> None:
    async with scratch_database(
        "scimadm", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.sessions() as session:
            admin = _user("admin", role="admin", email=_PLATFORM_ADMIN, days=30)
            session.add(admin)
            await session.commit()
            admin_id = admin.id
        async with _client(db.sessions, admin, monkeypatch) as client:
            raw = (await client.post("/api/v1/scim/tokens", json={"name": "Entra"})).json()["token"]
            refused = await client.patch(
                f"{_SCIM}/Users/{admin_id}",
                json={"Operations": [{"op": "replace", "value": {"active": False}}]},
                headers={"Authorization": f"Bearer {raw}"},
            )
            assert refused.status_code == 400
            assert refused.json()["scimType"] == "mutability"
        async with db.sessions() as session:
            assert (await session.get(User, admin_id)).is_active is True


async def test_an_account_linked_by_username_alone_is_managed_by_its_identity_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # externalId is optional in SCIM 2.0: the link alone hands the account,
    # its email included, to the identity provider.
    from app.api.v1.settings import email_change_blocked
    from app.models.user import provisioning_source

    monkeypatch.setattr("app.services.email.is_email_configured", lambda: True)
    async with scratch_database(
        "scimlink", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.sessions() as session:
            admin = _user("admin", role="admin", email=_PLATFORM_ADMIN, days=30)
            local = _user("local", email="local.person@example.com")
            session.add_all([admin, local])
            await session.commit()
            local_id = local.id
            assert provisioning_source(local) == "local" and email_change_blocked(local) is None
        async with _client(db.sessions, admin, monkeypatch) as client:
            raw = (await client.post("/api/v1/scim/tokens", json={"name": "Entra"})).json()["token"]
            linked = await client.post(
                f"{_SCIM}/Users",
                json={"userName": "Local.Person@example.com", "active": True},
                headers={"Authorization": f"Bearer {raw}"},
            )
            assert linked.status_code == 201 and linked.json()["id"] == str(local_id)
        async with db.sessions() as session:
            account = await session.get(User, local_id)
            assert account.scim_external_id is None and account.scim_linked_at is not None
            assert provisioning_source(account) == "scim"
            assert email_change_blocked(account) == "identity_provider"


async def test_the_upgrade_stamps_the_accounts_scim_already_manages() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "u1906scimlnk01_add_users_scim_linked_at.py"
    spec = importlib.util.spec_from_file_location("u1906scimlnk01", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with scratch_database(
        "scimstamp", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.sessions() as session:
            admin = _user("admin", role="admin", email=_PLATFORM_ADMIN, days=30)
            by_external_id = _user("ext")
            by_external_id.scim_external_id = "ext-1"
            by_audit, untouched = _user("audited"), _user("local")
            session.add_all([admin, by_external_id, by_audit, untouched])
            await session.flush()
            session.add(
                AuditLog(
                    user_id=by_audit.id,
                    user_email=by_audit.email,
                    action="update",
                    entity_type="user",
                    entity_id=by_audit.id,
                    summary="SCIM linked the existing account",
                    details={"linked": True, "scim_token_id": str(uuid.uuid4())},
                )
            )
            await session.commit()
            ids = {"ext": by_external_id.id, "audited": by_audit.id, "local": untouched.id, "admin": admin.id}
        async with db.sessions() as session:
            conn = await session.connection()
            stamped = await conn.run_sync(lambda sync: migration.backfill_in_batches(sync, batch_size=1))
            again = await conn.run_sync(lambda sync: migration.backfill_in_batches(sync, batch_size=1))
            await session.commit()
            assert (stamped, again) == (2, 0)
            linked = {name: (await session.get(User, uid)).scim_linked_at is not None for name, uid in ids.items()}
            assert linked == {"ext": True, "audited": True, "local": False, "admin": False}
