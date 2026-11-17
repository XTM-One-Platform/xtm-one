"""A deactivated account cannot authenticate, whichever credential it holds.

Deactivation keeps a user's per-user share grants: it is a reversible
toggle, and the kept grants confer nothing meanwhile only because a disabled
account cannot authenticate. ``user_visible_filter`` never looks at the
caller's account state, so every consumer of a credential has to refuse the
account itself. These pin the trusted-platform JWT branch of
``get_current_user_ungated``, and the web chat: its routes go through that
dependency, and a turn checks the account again before it starts, since it
outlives the request that authenticated it.

Mirrors the repo's fake-session convention: the real dependency, routes and
connection runner, with only the licence gate, the rate limiter and the
database session stubbed.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import settings

# ---------------------------------------------------------------------------
# Trusted-platform JWTs (REST)
# ---------------------------------------------------------------------------


class _EmailLookup:
    """Answers ``_resolve_trusted_platform_jwt``'s email lookup with *user*."""

    def __init__(self, user):
        self._user = user

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._user)


def _signed_by_a_trusted_platform(monkeypatch, email: str) -> None:
    """Skip the EdDSA verification: the platform vouches for *email*."""

    async def _verify(_token, _db):
        return SimpleNamespace(email=email, iss="https://opencti.example")

    monkeypatch.setattr("app.core.xtm_auth.verify_xtm_jwt", _verify)
    monkeypatch.setattr(pyjwt, "get_unverified_header", lambda _t: {"alg": "EdDSA"})


async def test_platform_jwt_of_a_deactivated_account_is_refused(monkeypatch):
    """Still active on the connected platform, deactivated here: refused like a
    session token, not signed in as the account."""
    _signed_by_a_trusted_platform(monkeypatch, "former@example.com")
    former = SimpleNamespace(id=uuid.uuid4(), email="former@example.com", is_active=False)

    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_ungated(
            credentials=SimpleNamespace(credentials="platform-jwt"),
            db=_EmailLookup(former),
        )

    assert exc.value.status_code == 401


async def test_platform_jwt_of_an_active_account_still_signs_it_in(monkeypatch):
    _signed_by_a_trusted_platform(monkeypatch, "colleague@example.com")
    colleague = SimpleNamespace(id=uuid.uuid4(), email="colleague@example.com", is_active=True)

    user = await deps.get_current_user_ungated(
        credentials=SimpleNamespace(credentials="platform-jwt"),
        db=_EmailLookup(colleague),
    )

    assert user is colleague
    assert user.auth_credential_kind == "platform_jwt"


# ---------------------------------------------------------------------------
# Web chat
# ---------------------------------------------------------------------------


class _Account:
    """The ``users`` row the chat reads - mutable, like the real one."""

    def __init__(self, *, is_active: bool = True, password_changed_at: datetime | None = None):
        self.id = uuid.uuid4()
        self.is_active = is_active
        self.password_changed_at = password_changed_at


class _ChatSession:
    """Just enough ``AsyncSession`` for the chat's own lookups.

    ``first()`` answers the account query (``None`` = the account is gone);
    ``scalar_one_or_none()`` answers the user lookup of the request's
    authentication with the account, and the turn's conversation lookup with
    "not found", which is how a turn shows it got past the account check;
    ``get()`` finds no companion PIN lock.
    """

    def __init__(self, account: _Account | None, *, authenticating: bool = False):
        self._account = account
        self._authenticating = authenticating

    async def get(self, _model, _key):
        # No companion PIN lock row: the session is not a phone's.
        return None

    async def execute(self, _stmt):
        account = self._account
        found = account if self._authenticating else None
        return SimpleNamespace(first=lambda: account, scalar_one_or_none=lambda: found)


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _web_chat_send(monkeypatch, account: _Account, *, issued_at: int | None):
    """``POST /web/chat/messages`` with a session token of *account* issued at *issued_at*."""
    import app.api.v1.platform as platform_api
    from app.api.v1 import chat_web_stream
    from app.core.database import get_db
    from app.core.security import create_access_token

    async def _chat_enabled(_db):
        return True

    async def _db():
        yield _ChatSession(account, authenticating=True)

    monkeypatch.setattr(platform_api, "is_chat_enabled", _chat_enabled)
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)
    token = create_access_token(str(account.id), extra={"iat": issued_at} if issued_at else None)
    app = FastAPI()
    app.include_router(chat_web_stream.router, prefix="/api/v1/web")
    app.dependency_overrides[get_db] = _db
    return TestClient(app).post(
        "/api/v1/web/chat/messages",
        json={"content": "hello", "conversation_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_deactivated_account_cannot_send_on_the_web_chat(monkeypatch):
    """A session token issued before the deactivation is still unexpired for up
    to ``JWT_EXPIRE_MINUTES``: the web chat refuses it like any REST route."""
    assert _web_chat_send(monkeypatch, _Account(is_active=False), issued_at=None).status_code == 401


def test_an_active_account_gets_past_authentication_on_the_web_chat(monkeypatch):
    assert _web_chat_send(monkeypatch, _Account(is_active=True), issued_at=None).status_code != 401


def test_a_token_issued_before_the_last_password_change_cannot_send_on_the_web_chat(monkeypatch):
    """Changing or resetting a password ends every older session, the web chat's included."""
    changed = datetime.now(UTC)
    account = _Account(is_active=True, password_changed_at=changed)
    before = int((changed - timedelta(hours=1)).timestamp())
    assert _web_chat_send(monkeypatch, account, issued_at=before).status_code == 401


async def _run_turn(monkeypatch, account: _Account, *, issued_at: int | None, deactivate: bool = False):
    """One send through the real connection runner, as *account*: the frames it wrote, and its refusal."""
    import app.core.database as database
    from app.api.v1 import chat as chat_module
    from app.services.chat_web_transport import HeadlessTurnConnection

    async def _no_rate_limit(*_a, **_kw):
        return None

    @asynccontextmanager
    async def _session():
        yield _ChatSession(account)

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _session_holds)
    monkeypatch.setattr(chat_module, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(database, "async_session", _session)
    frames: list[dict] = []

    async def _written(message):
        body = (message.get("body") or b"").decode()
        frames.extend(json.loads(c[6:]) for c in body.split("\n\n") if c.startswith("data: "))

    conversation_id = str(uuid.uuid4())
    connection = HeadlessTurnConnection(conversation_id, {"content": "summarise the knowledge base shared with me"})
    connection.bind(_written)
    auth = chat_module.ChatConnectionAuth(
        user_id=account.id, session=SimpleNamespace(), token_issued_at=issued_at
    )
    if deactivate:
        account.is_active = False
    await chat_module.run_chat_connection(connection, conversation_id, auth)
    return frames, connection.refusal


async def test_a_turn_does_not_start_once_the_account_is_deactivated(monkeypatch):
    """A turn outlives the request that authenticated it: an admin deactivating
    the account in between must stop the turn before any work is done."""
    frames, refusal = await _run_turn(monkeypatch, _Account(is_active=True), issued_at=_now(), deactivate=True)
    assert frames == []
    assert (refusal.status_code, refusal.detail) == (401, "Invalid token")


async def test_a_turn_of_an_active_account_runs(monkeypatch):
    frames, refusal = await _run_turn(monkeypatch, _Account(is_active=True), issued_at=_now())
    # Past the account check: the stubbed conversation lookup answers next.
    assert frames == []
    assert (refusal.status_code, refusal.detail) == (404, "Conversation not found")


_CHANGED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_CHANGED_TS = int(_CHANGED.timestamp())


@pytest.mark.parametrize(
    ("account", "issued_at", "expected"),
    [
        (None, _CHANGED_TS, "Invalid token"),
        (_Account(is_active=False), _CHANGED_TS, "Invalid token"),
        (_Account(is_active=True), None, None),
        (_Account(is_active=True, password_changed_at=_CHANGED), _CHANGED_TS, None),
        (_Account(is_active=True, password_changed_at=_CHANGED), _CHANGED_TS - 1, "Session expired"),
        (_Account(is_active=True, password_changed_at=_CHANGED), None, "Session expired"),
    ],
    ids=[
        "account-gone",
        "deactivated",
        "active",
        "issued-at-the-change",
        "issued-before-the-change",
        "no-iat-after-a-change",
    ],
)
async def test_account_rejection_mirrors_the_rest_session_checks(account, issued_at, expected):
    from app.api.v1.chat import resolve_account_rejection

    assert await resolve_account_rejection(_ChatSession(account), uuid.uuid4(), issued_at) == expected


@pytest.mark.parametrize(
    ("issued_at", "expected"),
    [(_CHANGED_TS + 60, None), (_CHANGED_TS - 60, "Session expired")],
    ids=["issued-after-the-change", "issued-before-the-change"],
)
async def test_a_send_hands_its_turn_the_issued_at_its_authentication_verified(monkeypatch, issued_at, expected):
    """Read from the verified claims, never from a second decode of a header
    the request may not carry: the turn's account check sees the token's
    ``iat`` against the last password change."""
    from app.api.v1 import chat as chat_module
    from app.api.v1 import chat_web_stream

    account = _Account(is_active=True, password_changed_at=_CHANGED)
    conv = SimpleNamespace(id=uuid.uuid4())
    seen: list = []

    async def _owned(_db, _user, _conversation_id):
        return conv

    async def _release(_db):
        return None

    async def _run(_connection, _conversation_id, auth):
        seen.append(auth)

    monkeypatch.setattr(chat_web_stream, "owned_conversation", _owned)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    monkeypatch.setattr(chat_module, "run_chat_connection", _run)
    claims = {"sub": str(account.id), "iat": issued_at, "impersonated_by": "an-admin"}
    user = SimpleNamespace(id=account.id, web_session=SimpleNamespace(claims=claims))

    response = await chat_web_stream.send_web_chat_message(
        chat_web_stream.WebChatSendRequest(content="hello", conversation_id=str(conv.id)),
        SimpleNamespace(headers={}),
        db=None,
        user=user,
    )

    async def _receive():
        return {"type": "http.disconnect"}

    async def _send(_message):
        return None

    await response({"type": "http"}, _receive, _send)
    [auth] = seen
    assert (auth.token_issued_at, auth.impersonated_by) == (issued_at, "an-admin")
    assert await chat_module.resolve_account_rejection(_ChatSession(account), account.id, auth.token_issued_at) == expected
