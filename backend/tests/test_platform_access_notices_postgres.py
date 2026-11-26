"""``POST /api/v1/platform/access-notices`` end to end, against real PostgreSQL.

Driven over HTTP through the real router and the real ``get_current_user``:
only a cross-platform JWT signed by an active registration sends a notice; a
session and an API key of the very same administrator are refused. Its three
limits answer 429 with ``Retry-After``; without an email server the notice
answers ``email_sent: false``. An email no account holds is invited by an
administrator, once (a second call reuses the pending invitation), and the
person who registers from it is sent on to the platform; without an email
server, from the ``link`` the invitation answers.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours. Redis is the in-memory fake of ``test_api_key_access.py``; the
platform's JWKS is served from XTM One's own key, as in
``test_platform_token_identity.py``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import api_keys, auth
from app.api.v1 import platform_access_notices as route
from app.core import rate_limit
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.core.xtm_auth import _kid, _private_key, _public_key_raw
from app.models.api_key import UserApiKey
from app.models.app_lock import AppLockSession
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.platform_registration import PlatformRegistration
from app.models.user import User
from app.services import api_key_access, email, token_store
from tests.scratch_database import scratch_database
from tests.test_api_key_access import _FakeRedis

pytestmark = pytest.mark.integration

XTM_ONE = "https://xtm-one.example.com"
OPENCRQ = "https://opencrq.example.com"
NOTICES = "/api/v1/platform/access-notices"


class _Redis(_FakeRedis):
    """The rate-limit counters, plus the invitations ``token_store`` keeps."""

    def __init__(self):
        super().__init__()
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.expiry[key] = ex

    async def get(self, key):
        return self.store[key] if key in self.store else self.values.get(key)

    async def getdel(self, key):
        self.expiry.pop(key, None)
        return self.store.pop(key, None)

    async def eval(self, script, numkeys, key, *args):
        if script != token_store.REISSUE_INVITATION_LUA:
            return await super().eval(script, numkeys, key, *args)
        new_key, expected, value, ttl = args
        if self.store.get(key) != expected:
            return 0
        await self.getdel(key)
        await self.set(new_key, value, ex=ttl)
        return 1

    async def ttl(self, key):
        return self.expiry.get(key, -2) if key in self.store else 30

    async def scan_iter(self, match):
        prefix = match.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key

    def invitations(self) -> list[str]:
        return [key for key in self.store if key.startswith("invite:")]


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """XTM One's identity, an in-memory Redis, the platform's keys, a recording mail server."""
    monkeypatch.setattr(settings, "base_url", XTM_ONE)
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])
    monkeypatch.setattr(settings, "smtp_hostname", "smtp.example.com")
    # A session must reach the route to be refused by it, not by the Terms of Use gate.
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)

    redis = _Redis()

    async def _redis():
        return redis

    monkeypatch.setattr(rate_limit, "redis_client", _redis)
    monkeypatch.setattr(api_key_access, "redis_client", _redis)
    monkeypatch.setattr(token_store, "redis_client", _redis)
    # Registering looks up the default agent of an XTM One install, a table this test does not create.
    monkeypatch.setattr(type(settings), "is_xtm_one", property(lambda _self: False))
    monkeypatch.setattr("app.core.redis.redis_client", _redis)

    class _Keys:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda url, **_kwargs: _Keys(url))

    delivered: list[dict] = []

    async def _deliver(**kwargs):
        delivered.append(kwargs)

    monkeypatch.setattr(email, "deliver_email", _deliver)
    return SimpleNamespace(redis=redis, delivered=delivered)


@asynccontextmanager
async def _database():
    async with scratch_database(
        "access_notices",
        tables=[
            User.__table__,
            Group.__table__,
            GroupMember.__table__,
            UserApiKey.__table__,
            PlatformRegistration.__table__,
            AuditLog.__table__,
            AppLockSession.__table__,
        ],
    ) as scratch:
        async with scratch.sessions() as db:
            db.add(
                PlatformRegistration(
                    platform_identifier="opencrq",
                    platform_url=OPENCRQ,
                    platform_title="OpenCRQ",
                    platform_version="1.0.0",
                    status="active",
                )
            )
            await db.commit()
        yield scratch


@asynccontextmanager
async def _client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1/platform")
    app.include_router(auth.router, prefix="/api/v1/auth")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _people(db, *names: str, active: bool = True) -> list[User]:
    people = [
        User(
            email=f"{name}-{uuid.uuid4().hex[:6]}@acme.com",
            hashed_password="x",
            full_name=name.title(),
            role="admin",
            is_active=active,
        )
        for name in names
    ]
    db.add_all(people)
    await db.commit()
    return people


def _platform_jwt(user: User, *, iss: str = OPENCRQ) -> dict[str, str]:
    """The token OpenCRQ signs for *user*, its administrator."""
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {
            "iss": iss,
            "sub": str(uuid.uuid4()),
            "email": user.email,
            "aud": XTM_ONE,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": str(uuid.uuid4()),
        },
        _private_key,
        algorithm="EdDSA",
        headers={"kid": _kid},
    )
    return {"Authorization": f"Bearer {token}"}


def _session(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _api_key(db, user: User) -> dict[str, str]:
    raw = api_key_access.generate_api_key()
    db.add(
        UserApiKey(
            user_id=user.id,
            name="Automation",
            key_prefix=api_key_access.display_prefix(raw),
            key_hash=await api_keys._hash_key(raw),
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


def _notice(recipient: User, scope_name: str = "Acme Corp") -> dict[str, str]:
    return {"recipient_user_id": str(recipient.id), "scope_name": scope_name, "path": "/t-123"}


async def test_only_a_registered_platforms_token_sends_a_notice(_world):
    async with _database() as scratch:
        async with scratch.sessions() as db:
            admin, jane = await _people(db, "claire", "jane")
            key = await _api_key(db, admin)

        async with _client(scratch.sessions) as client:
            for credential in (_session(admin), key):
                refused = await client.post(NOTICES, json=_notice(jane), headers=credential)
                assert refused.status_code == 403, refused.text
                assert refused.json()["detail"] == "Only a registered platform can send access notices"
            unknown_issuer = await client.post(
                NOTICES, json=_notice(jane), headers=_platform_jwt(admin, iss="https://evil.example.com")
            )
            assert unknown_issuer.status_code == 401, unknown_issuer.text
            assert (await client.post(NOTICES, json=_notice(jane))).status_code in (401, 403)
            assert _world.delivered == []

            sent = await client.post(NOTICES, json=_notice(jane), headers=_platform_jwt(admin))
            assert sent.status_code == 200, sent.text
            assert sent.json() == {"outcome": "notified", "email_sent": True, "user_id": str(jane.id), "link": None}

        [mail] = _world.delivered
        assert mail["to"] == jane.email
        assert mail["subject"] == "You have access to OpenCRQ"
        assert f'href="{OPENCRQ}/t-123"' in mail["html"]
        assert "Claire gave you access to Acme Corp on OpenCRQ" in mail["text"]
        async with scratch.sessions() as db:
            [audit] = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "access_notice"))).scalars()
            assert audit.user_id == admin.id


async def test_without_an_email_server_the_notice_answers_not_sent(_world, monkeypatch):
    monkeypatch.setattr(settings, "smtp_hostname", "")
    async with _database() as scratch:
        async with scratch.sessions() as db:
            admin, jane = await _people(db, "claire", "jane")

        async with _client(scratch.sessions) as client:
            answer = await client.post(NOTICES, json=_notice(jane), headers=_platform_jwt(admin))

        assert (answer.status_code, answer.json()) == (
            200,
            {"outcome": "notified", "email_sent": False, "user_id": str(jane.id), "link": None},
        )
        assert _world.delivered == []


async def test_an_unknown_or_deactivated_recipient_gets_one_answer(_world):
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [admin] = await _people(db, "claire")
            [gone] = await _people(db, "gone", active=False)

        async with _client(scratch.sessions) as client:
            answers = [
                await client.post(
                    NOTICES, json={**_notice(gone), "recipient_user_id": rid}, headers=_platform_jwt(admin)
                )
                for rid in (str(gone.id), str(uuid.uuid4()), "not-an-id")
            ]

        assert {(a.status_code, a.text) for a in answers} == {(404, '{"detail":"User not found"}')}
        assert _world.delivered == []


async def _assert_429(answer: httpx.Response) -> None:
    assert answer.status_code == 429, answer.text
    assert int(answer.headers["Retry-After"]) > 0


async def test_each_limit_answers_429_with_retry_after(_world, monkeypatch):
    monkeypatch.setattr(route, "SENDER_LIMIT_PER_HOUR", 2)
    monkeypatch.setattr(route, "PLATFORM_LIMIT_PER_HOUR", 3)
    async with _database() as scratch:
        async with scratch.sessions() as db:
            claire, marc, jane, paul, lea, tom = await _people(db, "claire", "marc", "jane", "paul", "lea", "tom")

        async with _client(scratch.sessions) as client:
            # One administrator: 2, then refused.
            for person in (jane, paul):
                assert (
                    await client.post(NOTICES, json=_notice(person), headers=_platform_jwt(claire))
                ).status_code == 200
            await _assert_429(await client.post(NOTICES, json=_notice(lea), headers=_platform_jwt(claire)))
            # The platform: 3 in all, the third by another administrator, then refused.
            assert (await client.post(NOTICES, json=_notice(lea), headers=_platform_jwt(marc))).status_code == 200
            await _assert_429(await client.post(NOTICES, json=_notice(tom), headers=_platform_jwt(marc)))

    monkeypatch.setattr(route, "SENDER_LIMIT_PER_HOUR", 200)
    monkeypatch.setattr(route, "PLATFORM_LIMIT_PER_HOUR", 1000)
    async with _database() as scratch:
        async with scratch.sessions() as db:
            claire, marc, jane = await _people(db, "claire", "marc", "jane")

        async with _client(scratch.sessions) as client:
            # One recipient: 5 an hour, whoever sends them.
            for sender in (claire, marc, claire, marc, claire):
                assert (
                    await client.post(NOTICES, json=_notice(jane), headers=_platform_jwt(sender))
                ).status_code == 200
            await _assert_429(await client.post(NOTICES, json=_notice(jane), headers=_platform_jwt(marc)))


INVITEE = "new.person@acme.com"


def _invite(path: str = "/t-123") -> dict[str, str]:
    return {"recipient_email": INVITEE, "scope_name": "Acme Corp", "path": path}


def _token_in(text: str) -> str:
    return re.search(r"/register\?token=([\w-]+)", text).group(1)


async def test_an_invited_person_registers_and_lands_on_the_platform(_world):
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [admin] = await _people(db, "claire")

        async with _client(scratch.sessions) as client:
            first = await client.post(NOTICES, json=_invite(), headers=_platform_jwt(admin))
            assert first.json() == {"outcome": "invited", "email_sent": True, "user_id": None, "link": None}, first.text
            # A minute left: the reissued link still gets a whole lifetime.
            [pending] = _world.redis.invitations()
            _world.redis.expiry[pending] = 60
            again = await client.post(NOTICES, json=_invite("/t-456"), headers=_platform_jwt(admin))
            assert again.json() == {"outcome": "invited", "email_sent": True, "user_id": None, "link": None}, again.text
            [reissued] = _world.redis.invitations()
            assert _world.redis.expiry[reissued] == 168 * 3600

            first_mail, second_mail = _world.delivered
            assert second_mail["subject"] == "You're invited to OpenCRQ"
            assert "(link valid for 7 days)" in second_mail["text"]
            account = {"password": "Correct-horse-9", "full_name": "New Person"}
            stale = await client.post(
                "/api/v1/auth/register-with-invitation", json={"token": _token_in(first_mail["text"]), **account}
            )
            assert stale.status_code == 410, stale.text
            registered = await client.post(
                "/api/v1/auth/register-with-invitation", json={"token": _token_in(second_mail["text"]), **account}
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["redirect_url"] == f"{OPENCRQ}/t-456"

            # Registered, the person is an account holder: notified from now on.
            notified = await client.post(NOTICES, json=_invite(), headers=_platform_jwt(admin))
            assert notified.json()["outcome"] == "notified"

        async with scratch.sessions() as db:
            person = (await db.execute(select(User).where(User.email == INVITEE))).scalar_one()
            assert person.role == "user"
            assert notified.json()["user_id"] == str(person.id)


async def test_without_an_email_server_the_invitation_answers_the_link_to_register_from(_world, monkeypatch):
    monkeypatch.setattr(settings, "smtp_hostname", "")
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [admin] = await _people(db, "claire")

        async with _client(scratch.sessions) as client:
            first = (await client.post(NOTICES, json=_invite(), headers=_platform_jwt(admin))).json()
            again = (await client.post(NOTICES, json=_invite("/t-456"), headers=_platform_jwt(admin))).json()
            assert (
                (first["outcome"], first["email_sent"]) == (again["outcome"], again["email_sent"]) == ("invited", False)
            )
            assert _world.delivered == []

            account = {"password": "Correct-horse-9", "full_name": "New Person"}
            stale = await client.post(
                "/api/v1/auth/register-with-invitation", json={"token": _token_in(first["link"]), **account}
            )
            assert stale.status_code == 410, stale.text
            registered = await client.post(
                "/api/v1/auth/register-with-invitation", json={"token": _token_in(again["link"]), **account}
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["redirect_url"] == f"{OPENCRQ}/t-456"


@pytest.mark.parametrize("role", ["admin", "user"])
async def test_an_invitation_made_on_the_invitations_page_is_left_alone(_world, role):
    """409, and the administrator's link still registers the person with the role it was made with."""
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [admin] = await _people(db, "claire")
        raw, _token_hash, _expires_at = await token_store.create_invitation(
            email=INVITEE, role=role, created_by_id=admin.id, ttl_hours=72
        )
        made = await token_store.get_invitation(raw)

        async with _client(scratch.sessions) as client:
            refused = await client.post(NOTICES, json=_invite(), headers=_platform_jwt(admin))
            assert refused.status_code == 409, refused.text
            assert await token_store.get_invitation(raw) == made
            assert len(_world.redis.invitations()) == 1
            assert _world.delivered == []

            registered = await client.post(
                "/api/v1/auth/register-with-invitation",
                json={"token": raw, "password": "Correct-horse-9", "full_name": "New Person"},
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["redirect_url"] is None

        async with scratch.sessions() as db:
            person = (await db.execute(select(User).where(User.email == INVITEE))).scalar_one()
            assert person.role == role


async def test_a_user_who_may_not_invite_creates_nothing(_world):
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [member] = await _people(db, "marc")
            member.role = "user"
            await db.commit()

        async with _client(scratch.sessions) as client:
            refused = await client.post(NOTICES, json=_invite(), headers=_platform_jwt(member))

        assert refused.status_code == 403, refused.text
        assert _world.redis.invitations() == []
        assert _world.delivered == []


async def test_a_deactivated_account_is_never_invited(_world):
    async with _database() as scratch:
        async with scratch.sessions() as db:
            [admin] = await _people(db, "claire")
            [gone] = await _people(db, "gone", active=False)

        async with _client(scratch.sessions) as client:
            answer = await client.post(
                NOTICES, json={**_invite(), "recipient_email": gone.email}, headers=_platform_jwt(admin)
            )

        assert (answer.status_code, answer.json()) == (404, {"detail": "User not found"})
        assert _world.redis.invitations() == []
        assert _world.delivered == []
