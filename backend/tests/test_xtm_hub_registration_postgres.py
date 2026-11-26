"""XTM One's registration with the XTM Hub against a real database.

The Hub is a fake ``xtm_hub_graphql``; everything else is real: the stored
registration (encrypted, bound to its Hub and platform id), the heartbeat's
compare-and-set (a check that started before a new registration never
overwrites or deletes it), the platform id changing under a registration,
and the API routes (admin gate, 422 before anything is sent, a refused or
failed registration writing nothing, the token never served, audit entries).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper
from app.api import deps
from app.api.v1 import xtm_hub_registration as routes
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.database import get_db
from app.core.security import decrypt_value
from app.core.xtm_hub_key import derive_key, key_version
from app.integrations.providers import xtm_hub as hub
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.xtm_hub_registration import (
    SINGLETON_ID,
    STATUS_LOST_CONNECTIVITY,
    STATUS_NEEDS_REREGISTRATION,
    STATUS_REGISTERED,
    XtmHubRegistration,
)
from app.services import xtm_hub_registration as registration
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_HUB = "https://hub.example.com"
# Computed, so no literal looks like a credential to a secret scanner.
_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:xtm-hub-platform-token"))
_NEW_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:xtm-hub-platform-token-2"))
_STRONG_SECRET = "x" * 24 + "-a-strong-test-secret-key"


class _FakeHub:
    """Answers every registration check with ``status`` (or ``err``) and records the calls."""

    def __init__(self, status: str | None = "active", err: str | None = None):
        self.status, self.err, self.calls = status, err, []
        self.credentials: list[dict] = []
        self.during_call = None

    async def __call__(self, base_url, query, variables, action, **credentials):
        self.calls.append((base_url, variables))
        self.credentials.append(credentials)
        if self.during_call is not None:
            hook, self.during_call = self.during_call, None
            await hook()
        if self.err:
            return None, self.err
        return {"refreshPlatformRegistrationConnectivityStatus": {"status": self.status}}, None


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("xtmhubreg", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _admin(sessions) -> User:
    async with sessions() as db:
        user = User(email="admin@acme.one.test", full_name="Ada Admin", role="admin", is_active=True)
        db.add(user)
        await db.commit()
        return user


async def _registration(sessions) -> XtmHubRegistration | None:
    async with sessions() as db:
        return await db.get(XtmHubRegistration, SINGLETON_ID)


async def _register(sessions, monkeypatch, token: str = _TOKEN) -> XtmHubRegistration:
    monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub("active"))
    async with sessions() as db:
        row = await registration.register(db, hub_url=_HUB, token=token)
        await db.commit()
        return row


@pytest.mark.asyncio
async def test_a_confirmed_registration_is_stored_encrypted_and_bound(monkeypatch):
    async with _database() as sessions:
        # When
        await _register(sessions, monkeypatch)

        # Then
        stored = await _registration(sessions)
        async with sessions() as db:
            platform_id = (await get_platform_settings(db)).id
        assert stored is not None
        assert (stored.hub_url, stored.platform_id, stored.status, stored.generation) == (
            _HUB,
            platform_id,
            STATUS_REGISTERED,
            1,
        )
        assert _TOKEN not in stored.token_enc
        assert decrypt_value(stored.token_enc) == _TOKEN
        assert stored.last_check_result == "active"


@pytest.mark.parametrize(
    ("status", "err", "message"),
    [
        ("inactive", None, "The XTM Hub did not confirm this registration."),
        ("not_found", None, "The XTM Hub did not confirm this registration."),
        (None, hub._NOT_A_HUB, "The XTM Hub did not confirm this registration."),
        (None, hub._UNREACHABLE, "The XTM Hub could not be reached."),
    ],
    ids=["inactive", "not-found", "not-a-hub", "unreachable"],
)
@pytest.mark.asyncio
async def test_an_unconfirmed_registration_writes_nothing(monkeypatch, status, err, message):
    async with _database() as sessions:
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub(status, err))
        async with sessions() as db:
            with pytest.raises(registration.RegistrationRefusedError) as refused:
                await registration.register(db, hub_url=_HUB, token=_TOKEN)
            await db.commit()
        assert str(refused.value).startswith(message)
        assert await _registration(sessions) is None


@pytest.mark.asyncio
async def test_registering_again_replaces_the_token_and_bumps_the_generation(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        await _register(sessions, monkeypatch, token=_NEW_TOKEN)
        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.generation, decrypt_value(stored.token_enc)) == (2, _NEW_TOKEN)


@pytest.mark.parametrize(
    ("status", "err", "expected_status", "expected_result"),
    [
        ("active", None, STATUS_REGISTERED, "active"),
        ("inactive", None, STATUS_LOST_CONNECTIVITY, "inactive"),
        (None, hub._UNREACHABLE, STATUS_LOST_CONNECTIVITY, "unreachable"),
    ],
    ids=["active", "inactive", "unreachable"],
)
@pytest.mark.asyncio
async def test_the_heartbeat_records_the_hub_answer(monkeypatch, status, err, expected_status, expected_result):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        fake = _FakeHub(status, err)
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        assert await registration.heartbeat() == expected_status

        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.status, stored.last_check_result) == (expected_status, expected_result)
        assert fake.calls[0][1]["input"]["token"] == _TOKEN


@pytest.mark.asyncio
async def test_a_registration_the_hub_no_longer_knows_is_forgotten_and_audited(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        await _register(sessions, monkeypatch)
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub("not_found"))

        assert await registration.heartbeat() is None

        assert await _registration(sessions) is None
        async with sessions() as db:
            entries = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "xtm_hub_registration")))
                .scalars()
                .all()
            )
        assert [(e.user_id, e.user_email, e.action) for e in entries] == [
            (admin.id, "system:xtm_hub_heartbeat", "delete")
        ]


@pytest.mark.asyncio
async def test_a_heartbeat_never_deletes_a_registration_made_while_it_ran(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        fake = _FakeHub("not_found")

        async def _register_again_meanwhile():
            # An admin registers again while the heartbeat waits on the Hub.
            async with sessions() as db:
                await db.execute(
                    update(XtmHubRegistration)
                    .where(XtmHubRegistration.id == SINGLETON_ID)
                    .values(generation=XtmHubRegistration.generation + 1)
                )
                await db.commit()

        fake.during_call = _register_again_meanwhile
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        await _admin(sessions)

        # The heartbeat reports the newer registration as it is, not the
        # forgetting it would have recorded, and audits no transition.
        assert await registration.heartbeat() == STATUS_REGISTERED

        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.generation, stored.status) == (2, STATUS_REGISTERED)
        async with sessions() as db:
            audited = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "xtm_hub_registration")))
                .scalars()
                .all()
            )
        assert audited == []


@pytest.mark.asyncio
async def test_registrations_at_once_each_get_their_own_generation(monkeypatch):
    async with _database() as sessions:
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub("active"))

        async def _one(token: str) -> int:
            async with sessions() as db:
                row = await registration.register(db, hub_url=_HUB, token=token)
                generation = row.generation
                await db.commit()
                return generation

        generations = await asyncio.gather(_one(_TOKEN), _one(_NEW_TOKEN))

        assert sorted(generations) == [1, 2]
        stored = await _registration(sessions)
        assert stored is not None
        assert stored.generation == 2


@pytest.mark.asyncio
async def test_a_registration_made_for_another_platform_id_sends_nothing(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        async with sessions() as db:
            await db.execute(
                update(XtmHubRegistration)
                .where(XtmHubRegistration.id == SINGLETON_ID)
                .values(platform_id=uuid.uuid4())
            )
            await db.commit()
        fake = _FakeHub("active")
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        assert await registration.heartbeat() == STATUS_NEEDS_REREGISTRATION
        assert await registration.heartbeat() == STATUS_NEEDS_REREGISTRATION

        assert fake.calls == []
        stored = await _registration(sessions)
        assert stored is not None
        assert stored.status == STATUS_NEEDS_REREGISTRATION


@pytest.mark.asyncio
async def test_the_heartbeat_restores_a_registration_once_the_platform_id_matches_again(monkeypatch):
    # The manual check and the heartbeat follow one rule: a registration whose
    # platform id matches again is the Hub's to confirm.
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        async with sessions() as db:
            platform_id = (await get_platform_settings(db)).id
            await db.execute(
                update(XtmHubRegistration)
                .where(XtmHubRegistration.id == SINGLETON_ID)
                .values(platform_id=uuid.uuid4())
            )
            await db.commit()
        fake = _FakeHub("active")
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        assert await registration.heartbeat() == STATUS_NEEDS_REREGISTRATION
        async with sessions() as db:
            await db.execute(
                update(XtmHubRegistration)
                .where(XtmHubRegistration.id == SINGLETON_ID)
                .values(platform_id=platform_id)
            )
            await db.commit()

        assert await registration.heartbeat() == STATUS_REGISTERED

        assert len(fake.calls) == 1
        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.status, stored.last_check_result) == (STATUS_REGISTERED, "active")


@pytest.mark.asyncio
async def test_the_heartbeat_holds_no_transaction_while_the_hub_answers(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        opened = []
        factory = registration.async_session

        @asynccontextmanager
        async def _recording():
            async with factory() as db:
                opened.append(db)
                yield db

        monkeypatch.setattr(registration, "async_session", _recording)
        fake = _FakeHub("active")
        during: list[bool] = []

        async def _look():
            during.extend(db.in_transaction() for db in opened)

        fake.during_call = _look
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        assert await registration.heartbeat() == STATUS_REGISTERED

        assert during == [False]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _app(user) -> FastAPI:
    application = FastAPI()
    application.include_router(routes.router, prefix="/api/v1/xtm-hub")
    application.dependency_overrides[deps.get_current_user] = lambda: user
    return application


@asynccontextmanager
async def _client(user) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=_app(user)), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_only_admins_read_or_change_the_registration_and_anyone_reads_the_status():
    async with _database() as _sessions:
        member = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user", email="member@acme.one.test")
        async with _client(member) as client:
            assert (await client.get("/api/v1/xtm-hub/registration")).status_code == 403
            assert (await client.post("/api/v1/xtm-hub/registration", json={"hub_url": _HUB, "token": _TOKEN})).status_code == 403
            assert (await client.post("/api/v1/xtm-hub/registration/check")).status_code == 403
            assert (await client.delete("/api/v1/xtm-hub/registration")).status_code == 403
            status = await client.get("/api/v1/xtm-hub/registration/status")
        assert (status.status_code, status.json()) == (
            200,
            {"status": "unregistered", "hub_url": None, "default_hub_url": hub.xtm_hub_default_url()},
        )


@pytest.mark.asyncio
async def test_check_now_records_the_answer_with_no_transaction_open_during_the_hub_call(monkeypatch):
    # As in production, the admin is loaded by the request's own session: the
    # transaction ended before the Hub call must leave it readable for the
    # audit entry written after it.
    async with _database() as sessions:
        admin_id = (await _admin(sessions)).id
        await _register(sessions, monkeypatch)
        opened = []

        async def _request_db():
            async with sessions() as db:
                opened.append(db)
                yield db

        async def _request_admin(db=Depends(get_db)):
            return await db.get(User, admin_id)

        application = FastAPI()
        application.include_router(routes.router, prefix="/api/v1/xtm-hub")
        application.dependency_overrides[get_db] = _request_db
        application.dependency_overrides[deps.get_current_user] = _request_admin
        fake = _FakeHub("inactive")
        during: list[bool] = []

        async def _look():
            during.extend(db.in_transaction() for db in opened)

        fake.during_call = _look
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
            response = await client.post("/api/v1/xtm-hub/registration/check")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == STATUS_LOST_CONNECTIVITY
        assert during == [False]
        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.status, stored.last_check_result) == (STATUS_LOST_CONNECTIVITY, "inactive")
        async with sessions() as db:
            audits = (
                await db.execute(
                    select(AuditLog.user_id, AuditLog.action).where(
                        AuditLog.entity_type == "xtm_hub_registration", AuditLog.action == "update"
                    )
                )
            ).all()
        assert [(row.user_id, row.action) for row in audits] == [(admin_id, "update")]


@pytest.mark.parametrize(
    ("hub_status", "expected_status", "expected_action"),
    [("not_found", "unregistered", "delete"), ("inactive", STATUS_LOST_CONNECTIVITY, "update")],
    ids=["forgotten", "lost"],
)
@pytest.mark.asyncio
async def test_check_now_audits_a_forgotten_registration_as_the_heartbeat_does(
    monkeypatch, hub_status, expected_status, expected_action
):
    async with _database() as sessions:
        admin = await _admin(sessions)
        await _register(sessions, monkeypatch)
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub(hub_status))
        async with _client(admin) as client:
            response = await client.post("/api/v1/xtm-hub/registration/check")

        assert (response.status_code, response.json()["status"]) == (200, expected_status)
        async with sessions() as db:
            actions = (
                (
                    await db.execute(
                        select(AuditLog.action).where(
                            AuditLog.entity_type == "xtm_hub_registration", AuditLog.user_id == admin.id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert actions == [expected_action]


@pytest.mark.asyncio
async def test_an_invalid_body_is_a_422_and_reaches_no_hub(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        fake = _FakeHub("active")
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        async with _client(admin) as client:
            bad_token = await client.post("/api/v1/xtm-hub/registration", json={"hub_url": _HUB, "token": "nope"})
            plain_http = await client.post(
                "/api/v1/xtm-hub/registration", json={"hub_url": "http://hub.example.com", "token": _TOKEN}
            )
        assert (bad_token.status_code, plain_http.status_code) == (422, 422)
        assert fake.calls == []


@pytest.mark.asyncio
async def test_a_refused_registration_answers_a_fixed_sentence_and_writes_nothing(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub("inactive"))
        async with _client(admin) as client:
            response = await client.post("/api/v1/xtm-hub/registration", json={"hub_url": _HUB, "token": _TOKEN})
        assert response.status_code == 400
        assert response.json() == {
            "detail": "The XTM Hub did not confirm this registration. Register again from the XTM Hub page."
        }
        assert await _registration(sessions) is None


@pytest.mark.asyncio
async def test_registering_serves_the_registration_but_never_the_token(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub("active"))
        async with _client(admin) as client:
            created = await client.post("/api/v1/xtm-hub/registration", json={"hub_url": _HUB, "token": _TOKEN})
            read = await client.get("/api/v1/xtm-hub/registration")
            forgotten = await client.delete("/api/v1/xtm-hub/registration")

        assert created.status_code == 200
        body = created.json()
        assert (body["status"], body["hub_url"], body["register_params"]["platform_identifier"]) == (
            "registered",
            _HUB,
            "xtmone",
        )
        for response in (created, read, forgotten):
            assert _TOKEN not in json.dumps(response.json())
        assert forgotten.json()["status"] == "unregistered"
        assert await _registration(sessions) is None
        async with sessions() as db:
            actions = (
                (
                    await db.execute(
                        select(AuditLog.action)
                        .where(AuditLog.entity_type == "xtm_hub_registration")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert actions == ["create", "delete"]


class _FakeAutoRegisterHub:
    """Answers ``autoRegisterPlatform`` with ``success`` (or ``err``) and records each call and its credential."""

    def __init__(self, success: bool = True, err: str | None = None):
        self.success, self.err, self.calls = success, err, []

    async def __call__(self, base_url, query, variables, action, **credentials):
        self.calls.append((base_url, query, variables, credentials))
        if self.err:
            return None, self.err
        return {"autoRegisterPlatform": {"success": self.success}}, None


def _auto_settings(monkeypatch, hub_url: str = _HUB) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "xtm_hub_default_url", hub_url)
    monkeypatch.setattr(settings, "base_url", "https://xtm-one.acme.one.test")
    monkeypatch.setattr(settings, "admin_email", "admin@acme.one.test")


@pytest.mark.asyncio
async def test_the_deployer_registers_the_platform_with_the_default_hub(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        async with sessions() as db:
            db.add(User(email="Analyst@Acme.One.Test", full_name="Ann Analyst", role="user", is_active=False))
            await db.commit()
            platform_id = (await get_platform_settings(db)).id
            await db.commit()
        _auto_settings(monkeypatch)
        from app.services import branding

        monkeypatch.setattr(branding, "get_cached_branding", lambda: SimpleNamespace(name="Acme XTM One"))
        fake = _FakeAutoRegisterHub()
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        async with _client(admin) as client:
            response = await client.put("/api/v1/xtm-hub/registration/auto", json={"token": f" {_TOKEN.upper()} "})

        assert (response.status_code, response.content) == (204, b"")
        ((base_url, query, variables, credentials),) = fake.calls
        assert (base_url, query) == (_HUB, registration.AUTO_REGISTER_MUTATION)
        assert credentials == {"platform_token": (str(platform_id), _TOKEN)}
        assert variables == {
            "input": {
                "platform": {
                    "id": str(platform_id),
                    "url": "https://xtm-one.acme.one.test",
                    "title": "Acme XTM One",
                    "contract": "CE",
                    "version": registration.registration_platform_version(),
                },
                # The deactivated analyst counts; the bootstrap admin does not.
                "existing_users_count": 1,
            }
        }
        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.hub_url, stored.platform_id, stored.status) == (_HUB, platform_id, STATUS_REGISTERED)
        assert decrypt_value(stored.token_enc) == _TOKEN
        async with sessions() as db:
            audits = (
                await db.execute(
                    select(AuditLog.action, AuditLog.user_id).where(AuditLog.entity_type == "xtm_hub_registration")
                )
            ).all()
        assert [tuple(row) for row in audits] == [("create", admin.id)]


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (_FakeAutoRegisterHub(success=False), "The XTM Hub did not accept this registration"),
        (_FakeAutoRegisterHub(err=hub._REFUSED), "The XTM Hub did not accept this registration"),
        (_FakeAutoRegisterHub(err=hub._NOT_A_HUB), "The XTM Hub did not accept this registration"),
        (_FakeAutoRegisterHub(err=hub._UNREACHABLE), "The XTM Hub could not be reached"),
    ],
    ids=["not-a-success", "refused", "not-a-hub", "unreachable"],
)
@pytest.mark.asyncio
async def test_an_automatic_registration_the_hub_refuses_is_a_502_and_writes_nothing(monkeypatch, fake, message):
    async with _database() as sessions:
        admin = await _admin(sessions)
        _auto_settings(monkeypatch)
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        async with _client(admin) as client:
            response = await client.put("/api/v1/xtm-hub/registration/auto", json={"token": _TOKEN})

        assert response.status_code == 502
        assert response.json()["detail"].startswith(message)
        assert await _registration(sessions) is None


@pytest.mark.asyncio
async def test_an_automatic_registration_is_refused_before_any_call(monkeypatch):
    async with _database() as sessions:
        admin = await _admin(sessions)
        member = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user", email="member@acme.one.test")
        fake = _FakeAutoRegisterHub()
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
        _auto_settings(monkeypatch)
        async with _client(member) as client:
            not_admin = await client.put("/api/v1/xtm-hub/registration/auto", json={"token": _TOKEN})
        async with _client(admin) as client:
            not_a_token = await client.put("/api/v1/xtm-hub/registration/auto", json={"token": "nope"})
            no_token = await client.put("/api/v1/xtm-hub/registration/auto", json={})
            _auto_settings(monkeypatch, hub_url="http://hub.example.com")
            plain_http = await client.put("/api/v1/xtm-hub/registration/auto", json={"token": _TOKEN})

        assert (not_admin.status_code, not_a_token.status_code, no_token.status_code, plain_http.status_code) == (
            403,
            400,
            422,
            502,
        )
        assert fake.calls == []
        assert await _registration(sessions) is None


async def _enroll(sessions) -> None:
    """Record the key enrollment the way the enrollment will: the date on the registration."""
    async with sessions() as db:
        await db.execute(
            update(XtmHubRegistration)
            .where(XtmHubRegistration.id == SINGLETON_ID)
            .values(key_enrolled_at=datetime.now(UTC))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_a_registration_starts_with_no_enrolled_key(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        stored = await _registration(sessions)
        assert stored is not None and stored.key_enrolled_at is None


@pytest.mark.asyncio
async def test_once_enrolled_the_heartbeat_is_signed_and_sends_no_token(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", _STRONG_SECRET)
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        await _enroll(sessions)
        fake = _FakeHub("active")
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        assert await registration.heartbeat() == STATUS_REGISTERED

        ((_base_url, variables),) = fake.calls
        (credentials,) = fake.credentials
        assert "token" not in variables["input"]
        assert set(credentials) == {"platform_assertion"}
        async with sessions() as db:
            platform_id = (await get_platform_settings(db)).id
            key = derive_key(await key_version(db))
        claims = pyjwt.decode(
            credentials["platform_assertion"],
            pyjwt.PyJWK(key.public_jwk()).key,
            algorithms=["EdDSA"],
            audience="xtm-hub",
            issuer=str(platform_id),
        )
        assert claims["exp"] - claims["iat"] == 60


@pytest.mark.asyncio
async def test_before_enrollment_the_heartbeat_sends_the_token(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        fake = _FakeHub("active")
        monkeypatch.setattr(hub, "xtm_hub_graphql", fake)

        assert await registration.heartbeat() == STATUS_REGISTERED

        assert fake.calls[0][1]["input"]["token"] == _TOKEN
        assert fake.credentials == [{}]


@pytest.mark.asyncio
async def test_a_key_the_hub_no_longer_matches_asks_for_a_new_registration(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", _STRONG_SECRET)
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        await _enroll(sessions)
        refusal = hub._refusal(["PLATFORM_KEY_MISMATCH"])
        monkeypatch.setattr(hub, "xtm_hub_graphql", _FakeHub(None, refusal))

        assert await registration.heartbeat() == STATUS_NEEDS_REREGISTRATION

        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.status, stored.last_check_result) == (STATUS_NEEDS_REREGISTRATION, "key_mismatch")


@pytest.mark.asyncio
async def test_a_new_registration_forgets_the_enrolled_key(monkeypatch):
    async with _database() as sessions:
        await _register(sessions, monkeypatch)
        await _enroll(sessions)

        await _register(sessions, monkeypatch, token=_NEW_TOKEN)

        stored = await _registration(sessions)
        assert stored is not None
        assert (stored.generation, stored.key_enrolled_at) == (2, None)

