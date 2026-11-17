"""The state-less GitHub App install callback over real PostgreSQL rows.

``test_github_app_install_callback_owner.py`` scripts the session. This drives
``oauth_callback`` over real rows: the candidate query, and the write made on
rows read again under ``FOR UPDATE`` once GitHub has answered - an edit made
to an integration while GitHub was being asked is kept, not overwritten by
the configuration read before. GitHub is ``httpx.MockTransport``. Every owner
is an ordinary user.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, update

import app.api.oauth_callbacks as callbacks
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.security import decrypt_value, encrypt_value
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ADMIN_CLIENT_ID = "admin-github-app-4360-pg"
INSTALLER_TOKEN = "ghu_installer"  # what the install code exchanges for
ACCOUNTS = {INSTALLER_TOKEN: 2002, "ghu_alice": 2002, "ghu_bob": 1001, "ghu_company": 2002}


def _scratch():
    return scratch_database(
        "github_install_callback",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    )


async def _seed(sessions) -> dict[str, uuid.UUID]:
    async with sessions() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="user",
            )
            for name in ("alice", "bob")
        }
        db.add_all(users.values())
        await db.flush()
        db.add(
            OAuthDefault(
                provider="github",
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value("admin-secret"),
            )
        )
        integrations = {
            name: Integration(
                user_id=users[name].id,
                provider="github",
                auth_type="oauth",
                name=f"{name}'s GitHub",
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value("secret"),
                access_token_enc=encrypt_value(f"ghu_{name}"),
                extra_config_enc=encrypt_value(json.dumps({"installations": [{"id": "111"}]})),
                status="connected",
            )
            for name in ("alice", "bob")
        }
        db.add_all(integrations.values())
        await db.commit()
        return {name: integration.id for name, integration in integrations.items()}


def _route(monkeypatch, on_user=None):
    real_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(200, json={"access_token": INSTALLER_TOKEN, "refresh_token": "r"})
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.url.path == "/user":
            if on_user is not None:
                await on_user(token)
            return httpx.Response(200, json={"id": ACCOUNTS[token], "login": token})
        if request.url.path == "/user/installations" and token == INSTALLER_TOKEN:
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "installations": [{"id": 222, "account": {"login": "org-222", "type": "Organization"}}],
                },
            )
        return httpx.Response(404)

    def _client(*args, **kwargs):
        kwargs.pop("limits", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(callbacks.httpx, "AsyncClient", _client)
    # The limiter has its own tests; this request is a stand-in with no client address.
    monkeypatch.setattr(callbacks, "enforce_rate_limit", AsyncMock())


async def _installations(sessions, integration_id) -> list[str]:
    async with sessions() as db:
        enc = (
            await db.execute(select(Integration.extra_config_enc).where(Integration.id == integration_id))
        ).scalar_one()
    return [i["id"] for i in json.loads(decrypt_value(enc))["installations"]]


async def _install(sessions):
    request = SimpleNamespace(
        query_params={"code": "install-code", "installation_id": "222", "setup_action": "install"}
    )
    async with sessions() as db:
        return await callbacks.oauth_callback(request, db=db)


@pytest.mark.asyncio
async def test_the_installation_lands_on_the_installers_row_only(monkeypatch):
    async with _scratch() as scratch:
        ids = await _seed(scratch.sessions)
        _route(monkeypatch)
        response = await _install(scratch.sessions)
        alice = await _installations(scratch.sessions, ids["alice"])
        bob = await _installations(scratch.sessions, ids["bob"])
        async with scratch.sessions() as db:
            audits = (await db.execute(select(AuditLog))).scalars().all()

    assert alice == ["111", "222"]
    assert bob == ["111"]
    assert response.headers["location"].endswith("/integrations?connected=github")
    # Written in the same transaction as the installation, by the platform.
    (audit,) = audits
    assert (audit.entity_type, audit.entity_id) == ("integration", ids["alice"])
    assert audit.user_email == "system:github_app_install"
    assert audit.details["installation_id"] == "222"


@pytest.mark.asyncio
async def test_an_edit_made_while_github_is_asked_is_kept(monkeypatch):
    async with _scratch() as scratch:
        ids = await _seed(scratch.sessions)

        async def _edit_meanwhile(token):
            if token != "ghu_alice":
                return
            async with scratch.sessions() as other:
                await other.execute(
                    update(Integration)
                    .where(Integration.id == ids["alice"])
                    .values(
                        extra_config_enc=encrypt_value(
                            json.dumps({"installations": [{"id": "111"}, {"id": "555"}], "webhook_secret": "s"})
                        )
                    )
                )
                await other.commit()

        _route(monkeypatch, on_user=_edit_meanwhile)
        await _install(scratch.sessions)
        async with scratch.sessions() as db:
            enc = (
                await db.execute(select(Integration.extra_config_enc).where(Integration.id == ids["alice"]))
            ).scalar_one()

    extra = json.loads(decrypt_value(enc))
    assert [i["id"] for i in extra["installations"]] == ["111", "555", "222"]
    assert extra["webhook_secret"] == "s"


@pytest.mark.asyncio
async def test_an_owner_less_company_managed_row_is_audited_on_the_oldest_admin(monkeypatch):
    """A real audit row, anchored on an admin: ``audit_logs.user_id`` is NOT NULL."""
    async with _scratch() as scratch:
        ids = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="admin", role="admin"
            )
            company = Integration(
                user_id=None,
                is_company_managed=True,
                provider="github",
                auth_type="oauth",
                name="Company GitHub",
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value("secret"),
                access_token_enc=encrypt_value("ghu_company"),
                extra_config_enc=encrypt_value(json.dumps({"installations": [{"id": "111"}]})),
                status="connected",
            )
            db.add_all([admin, company])
            await db.commit()
            admin_id, company_id = admin.id, company.id
        _route(monkeypatch)
        await _install(scratch.sessions)
        recorded = await _installations(scratch.sessions, company_id)
        async with scratch.sessions() as db:
            audits = {a.entity_id: a for a in (await db.execute(select(AuditLog))).scalars().all()}

    assert recorded == ["111", "222"]
    assert set(audits) == {ids["alice"], company_id}
    assert audits[company_id].user_id == admin_id
    assert audits[company_id].user_email == "system:github_app_install"
