"""Web session absolute lifetime and idle timeout.

Pins the contract of ``app/core/sessions.py``:

* every sign-in (local password, OIDC, SAML) mints a token carrying when the
  person signed in (``auth_time``) and its session (``sid``); a token that
  carries a session on (password change, impersonation) keeps both, so no
  re-mint extends the absolute lifetime;
* the absolute lifetime is checked against the setting in force when the token
  is presented: lowering it ends older sessions at once, in use or not;
* the idle timeout ends a session nobody used for that long, on REST, on the
  web chat (its routes, a turn about to start, its events stream) and on the
  activity stream; only the explicit activity signal moves its deadline, never
  a request;
* Redis unreachable: the idle timeout fails open, the lifetime still applies;
* the defaults sign nobody out on upgrade, tokens minted earlier included.

The real dependency, the real routes and the real JWTs, with only Redis (an
in-memory stand-in), the database session and the licence gate stubbed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import jwt as pyjwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.core import security_options as so
from app.core import sessions
from app.core.config import settings
from app.core.security import create_access_token, decode_access_token

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeRedis:
    """The two commands the session code sends, kept in a dict; ``broken`` raises like an outage."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.broken = False
        self.calls = 0

    async def get(self, key: str) -> str | None:
        self.calls += 1
        if self.broken:
            raise ConnectionError("Redis is down")
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        self.calls += 1
        if self.broken:
            raise ConnectionError("Redis is down")
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    monkeypatch.setattr(sessions, "_last_unenforced_warning", None)
    monkeypatch.setattr(sessions, "_redis_unreachable_until", 0.0)
    return fake


def _redis_due_another_try(monkeypatch) -> None:
    """The backoff after a Redis failure lapsed: the next request tries it again."""
    monkeypatch.setattr(sessions, "_redis_unreachable_until", time.monotonic() - 1)


def _now() -> int:
    return int(time.time())


def _set(**values) -> None:
    """What an administrator saves in Settings > Access and security (applies without a restart)."""
    so.publish_stored(values)


class _UserLookup:
    """Answers the user lookup of ``resolve_session_jwt`` with *account*; no companion PIN lock."""

    def __init__(self, account) -> None:
        self._account = account

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._account)

    async def get(self, _model, _key):
        return None


class _NotACompanion:
    """The database of ``/auth/session/activity``: no companion PIN lock to extend."""

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def commit(self) -> None:
        return None


def _account(user_id: str | uuid.UUID | None = None):
    return SimpleNamespace(id=uuid.UUID(str(user_id)) if user_id else uuid.uuid4(), is_active=True, password_changed_at=None)


async def _rest(token: str, account) -> object:
    """``get_current_user_ungated`` - the dependency every REST route depends on."""
    return await deps.get_current_user_ungated(credentials=SimpleNamespace(credentials=token), db=_UserLookup(account))


async def _rest_refusal(token: str, account) -> dict:
    with pytest.raises(HTTPException) as exc:
        await _rest(token, account)
    assert exc.value.status_code == 401
    return exc.value.detail


def _activity_key(token: str) -> str:
    return sessions.activity_key(decode_access_token(token), token)


# ---------------------------------------------------------------------------
# The claims every sign-in mints
# ---------------------------------------------------------------------------


def test_a_sign_in_starts_a_session_bounded_by_the_lifetime_in_force():
    _set(jwt_expire_minutes=480)
    before = _now()
    claims = decode_access_token(create_access_token(str(uuid.uuid4())))
    assert before <= claims["auth_time"] <= _now()
    assert claims["iat"] == claims["auth_time"]
    assert isinstance(claims["sid"], str) and len(claims["sid"]) >= 16
    assert claims["exp"] == claims["auth_time"] + 480 * 60


@pytest.mark.parametrize(("variable", "read"), [(0, 0), (1, 5), (4, 5), (5, 5), (600_000, 525_600)])
def test_the_idle_timeout_variable_is_off_or_at_least_five_minutes(variable, read):
    from app.core.config import Settings

    assert Settings(_env_file=None, session_idle_timeout_minutes=variable).session_idle_timeout_minutes == read


def test_two_sign_ins_are_two_sessions():
    first = decode_access_token(create_access_token("u"))
    second = decode_access_token(create_access_token("u"))
    assert first["sid"] != second["sid"]


def test_a_token_that_carries_a_session_on_never_extends_it():
    signed_in = _now() - 3600
    carried = {"sub": "u", "auth_time": signed_in, "sid": "the-session", "iat": signed_in}
    claims = decode_access_token(create_access_token("u", continues=carried))
    assert (claims["auth_time"], claims["sid"]) == (signed_in, "the-session")
    assert claims["exp"] == signed_in + settings.jwt_expire_minutes * 60
    assert claims["iat"] >= signed_in + 3600  # a fresh iat: survives the password change it follows


def test_a_token_that_carries_a_session_on_never_outlives_it_after_the_lifetime_was_raised():
    # Signed in under an 8-hour lifetime; an administrator then raised it to 7 days.
    signed_in = _now() - 3600
    carried = {"sub": "u", "auth_time": signed_in, "sid": "s", "iat": signed_in, "exp": signed_in + 8 * 3600}
    _set(jwt_expire_minutes=7 * 24 * 60)
    # A password change, an impersonation: the re-minted token ends with the session.
    assert decode_access_token(create_access_token("u", continues=carried))["exp"] == signed_in + 8 * 3600
    # The Desktop handoff carries the browser session's end the same way.
    handoff = decode_access_token(create_access_token("u", continues={"auth_time": signed_in, "exp": carried["exp"]}))
    assert handoff["exp"] == signed_in + 8 * 3600 and handoff["sid"] != "s"


def test_a_token_from_before_the_upgrade_carries_its_iat_on_as_its_sign_in():
    issued = _now() - 600
    claims = decode_access_token(create_access_token("u", continues={"sub": "u", "iat": issued}))
    assert claims["auth_time"] == issued
    assert claims["sid"]  # a session of its own from now on


def test_local_sign_in_and_password_change_keep_one_session(monkeypatch):
    from app.api.v1 import auth

    user = SimpleNamespace(id=uuid.uuid4())
    signed_in = decode_access_token(auth._build_token(user))
    user.web_session = SimpleNamespace(claims=signed_in)
    changed = decode_access_token(auth._build_token(user, continues=user))
    assert (changed["auth_time"], changed["sid"]) == (signed_in["auth_time"], signed_in["sid"])
    # An API key holder has no session to carry on: a new one starts.
    key_holder = SimpleNamespace(id=user.id)
    assert decode_access_token(auth._build_token(key_holder, continues=key_holder))["sid"] != signed_in["sid"]


@pytest.mark.parametrize("provider", ["oidc", "saml"])
def test_an_sso_sign_in_starts_a_session(monkeypatch, provider):
    import app.services.mfa as mfa
    from app.api import auth_sso

    monkeypatch.setattr(mfa, "challenge_required", lambda _user: False)
    user = SimpleNamespace(id=uuid.uuid4())
    url = auth_sso._build_frontend_redirect(user, provider=provider)
    token = parse_qs(urlsplit(url).fragment)["token"][0]
    claims = decode_access_token(token)
    assert claims["sub"] == str(user.id)
    assert claims["auth_time"] >= _now() - 5 and claims["sid"]


async def test_an_impersonation_carries_the_administrators_session_on(monkeypatch):
    from app.api.v1 import users

    admin_claims = {"sub": "admin", "auth_time": _now() - 120, "sid": "admin-session", "iat": _now() - 120}
    admin = SimpleNamespace(id=uuid.uuid4(), web_session=SimpleNamespace(claims=admin_claims))
    target = SimpleNamespace(id=uuid.uuid4(), is_active=True, full_name="T", email="t@example.test")

    class _Db:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: target)

        async def commit(self):
            return None

    monkeypatch.setattr(users, "log_audit", AsyncMock())
    response = await users.impersonate_user(str(target.id), request=SimpleNamespace(), admin=admin, db=_Db())
    claims = decode_access_token(response.access_token)
    assert claims["impersonated_by"] == str(admin.id)
    # It ends with the administrator's session, and shares its idle deadline.
    assert (claims["auth_time"], claims["sid"]) == (admin_claims["auth_time"], "admin-session")


# ---------------------------------------------------------------------------
# Absolute lifetime (REST)
# ---------------------------------------------------------------------------


async def test_the_defaults_sign_nobody_out_tokens_from_before_the_upgrade_included(redis):
    account = _account()
    issued = _now() - 6 * 24 * 3600  # six days ago, with the 7-day exp it was minted with
    legacy = pyjwt.encode(
        {"sub": str(account.id), "iat": issued, "exp": issued + 7 * 24 * 3600},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert await _rest(legacy, account) is account
    assert await _rest(create_access_token(str(account.id)), account) is account
    assert redis.store == {}  # no idle timeout by default: Redis is never read


async def test_a_session_past_its_lifetime_is_refused_even_while_in_use(redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id), continues={"auth_time": _now() - 3 * 3600})
    redis.store[_activity_key(token)] = str(_now())  # used a second ago
    assert await _rest(token, account) is account
    _set(jwt_expire_minutes=120, session_idle_timeout_minutes=30)
    detail = await _rest_refusal(token, account)
    assert detail == {
        "code": "session_expired",
        "reason": "lifetime",
        "message": "Your session has expired. Sign in again to continue.",
    }


async def test_lowering_the_lifetime_ends_sessions_already_issued(redis):
    account = _account()
    token = create_access_token(str(account.id), continues={"auth_time": _now() - 3 * 3600})
    assert await _rest(token, account) is account  # 7 days by default
    _set(jwt_expire_minutes=120)  # an administrator lowers it: no restart, no new token
    assert (await _rest_refusal(token, account))["reason"] == "lifetime"


async def test_a_token_minted_past_its_lifetime_is_an_ended_session(redis):
    _set(jwt_expire_minutes=60)
    account = _account()
    token = create_access_token(str(account.id), continues={"auth_time": _now() - 2 * 3600})
    assert (await _rest_refusal(token, account))["reason"] == "lifetime"


async def test_raising_the_lifetime_applies_from_the_next_sign_in(redis):
    _set(jwt_expire_minutes=60)
    account = _account()
    token = create_access_token(str(account.id), continues={"auth_time": _now() - 61 * 60})
    _set(jwt_expire_minutes=600)
    # The token's own exp was set at its sign-in: refused, and said so as an ended session.
    assert (await _rest_refusal(token, account))["reason"] == "lifetime"
    assert await _rest(create_access_token(str(account.id)), account) is account


async def test_a_confined_token_is_never_read_as_a_session(redis):
    # Refused as before ("Invalid token"); its activity is never read or written.
    _set(session_idle_timeout_minutes=5)
    confined = pyjwt.encode(
        {"sub": str(uuid.uuid4()), "scope": "mfa", "exp": _now() + 60}, settings.secret_key, algorithm="HS256"
    )
    assert await _rest_refusal(confined, _account()) == "Invalid token"
    assert redis.store == {}


def _expired_confined_token(claim: str) -> str:
    return pyjwt.encode(
        {"sub": str(uuid.uuid4()), claim: "mfa", "exp": _now() - 3600}, settings.secret_key, algorithm="HS256"
    )


@pytest.mark.parametrize("claim", ["scope", "type"])
async def test_an_expired_confined_token_is_not_an_ended_session(redis, claim):
    assert await _rest_refusal(_expired_confined_token(claim), _account()) == "Invalid token"


def test_only_an_expired_session_token_of_this_platform_ended_a_session():
    from app.core.security import is_expired_session_token

    expired = create_access_token(str(uuid.uuid4()), continues={"auth_time": _now() - 8 * 24 * 3600})
    assert is_expired_session_token(expired)
    assert not is_expired_session_token(_expired_confined_token("scope"))
    assert not is_expired_session_token(_expired_confined_token("type"))
    forged = pyjwt.encode({"sub": str(uuid.uuid4()), "exp": _now() - 3600}, "another-platform-key-of-hs256-length", algorithm="HS256")
    assert not is_expired_session_token(forged)
    assert not is_expired_session_token("not-a-token")


# ---------------------------------------------------------------------------
# Idle timeout (REST)
# ---------------------------------------------------------------------------


async def test_an_idle_session_is_refused_after_the_idle_timeout(redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    detail = await _rest_refusal(token, account)
    assert (detail["code"], detail["reason"]) == ("session_expired", "idle")


async def test_a_request_is_not_activity_but_the_activity_signal_is(redis):
    from app.api.v1 import auth

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    key = _activity_key(token)
    last = str(_now() - 20 * 60)
    redis.store[key] = last

    user = await _rest(token, account)
    status = await auth.get_session_status(user=user)
    assert redis.store[key] == last  # neither a request nor reading the status moves it
    assert status.kind == "session" and status.idle_timeout_seconds == 1800
    assert status.idle_expires_at == datetime.fromtimestamp(int(last) + 1800, UTC)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "enforce_rate_limit", AsyncMock())
        moved = await auth.record_session_activity(user=user, db=_NotACompanion())
    assert int(redis.store[key]) >= _now() - 1
    assert moved.idle_expires_at > status.idle_expires_at
    assert moved.expires_at == status.expires_at  # never the absolute lifetime


async def test_the_activity_signal_never_revives_an_ended_session(redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    key = _activity_key(token)
    last = str(_now() - 40 * 60)
    redis.store[key] = last
    # The dependency of POST /auth/session/activity refuses it first.
    assert (await _rest_refusal(token, account))["reason"] == "idle"
    assert redis.store[key] == last


async def test_a_session_with_no_recorded_activity_starts_counting_now(redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    assert await _rest(token, account) is account
    key = _activity_key(token)
    assert int(redis.store[key]) >= _now() - 1
    # Kept as long as the token can live, so a raised idle timeout never outlives its record.
    assert redis.ttl[key] >= decode_access_token(token)["exp"] - _now()


async def test_turning_the_idle_timeout_on_applies_to_sessions_already_issued(redis):
    from app.api.v1 import auth

    account = _account()
    token = create_access_token(str(account.id))
    user = await _rest(token, account)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "enforce_rate_limit", AsyncMock())
        await auth.record_session_activity(user=user, db=_NotACompanion())  # recorded even with no idle timeout
    key = _activity_key(token)
    redis.store[key] = str(_now() - 11 * 60)
    assert await _rest(token, account) is account
    _set(session_idle_timeout_minutes=10)
    assert (await _rest_refusal(token, account))["reason"] == "idle"


def test_an_impersonation_and_its_administrator_share_one_idle_deadline():
    admin, target = _account(), _account()
    admin_token = create_access_token(str(admin.id))
    impersonation = create_access_token(
        str(target.id), extra={"impersonated_by": str(admin.id)}, continues=decode_access_token(admin_token)
    )
    assert _activity_key(admin_token) == _activity_key(impersonation)


async def test_a_token_from_before_the_upgrade_is_idle_tracked_too(redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    legacy = pyjwt.encode(
        {"sub": str(account.id), "iat": _now() - 60, "exp": _now() + 3600},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert await _rest(legacy, account) is account
    key = _activity_key(legacy)
    assert key.startswith("session:activity:t:")
    redis.store[key] = str(_now() - 31 * 60)
    assert (await _rest_refusal(legacy, account))["reason"] == "idle"


async def test_redis_unreachable_fails_open_and_says_so(redis, caplog, monkeypatch):
    from app.api.v1 import auth

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    redis.broken = True
    with caplog.at_level("WARNING", logger="app.core.sessions"):
        user = await _rest(token, account)
        await _rest(token, account)
    assert user is account
    warnings = [r for r in caplog.records if "idle timeout is not enforced" in r.getMessage()]
    assert len(warnings) == 1  # once, not on every request
    status = await auth.get_session_status(user=user)
    assert (status.idle_enforced, status.idle_expires_at) == (False, None)
    # The absolute lifetime does not need Redis.
    old = create_access_token(str(account.id), continues={"auth_time": _now() - 8 * 24 * 3600})
    assert (await _rest_refusal(old, account))["reason"] == "lifetime"
    redis.broken = False
    _redis_due_another_try(monkeypatch)
    assert (await _rest_refusal(token, account))["reason"] == "idle"


async def test_after_a_redis_failure_requests_leave_it_aside_until_one_tries_again(redis, monkeypatch):
    from app.api.v1 import auth

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    redis.broken = True
    user = await _rest(token, account)
    assert redis.calls == 1
    # Within the backoff no request waits on Redis: reads and the activity signal fail open at once.
    await _rest(token, account)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "enforce_rate_limit", AsyncMock())
        recorded = await auth.record_session_activity(user=user, db=_NotACompanion())
    assert redis.calls == 1
    assert (recorded.idle_enforced, recorded.idle_expires_at) == (False, None)
    # Once it lapses, one request tries again; an answer ends the backoff.
    redis.broken = False
    _redis_due_another_try(monkeypatch)
    assert await _rest(token, account) is account
    assert redis.calls > 1 and sessions._redis_unreachable_until == 0.0


async def test_a_credential_that_is_not_a_web_session_has_no_deadline():
    from app.api.v1 import auth

    status = await auth.get_session_status(user=SimpleNamespace(auth_credential_kind="api_key"))
    assert (status.kind, status.expires_at, status.idle_timeout_seconds) == ("other", None, 0)
    recorded = await auth.record_session_activity(user=SimpleNamespace(auth_credential_kind="api_key"))
    assert recorded.kind == "other"


# ---------------------------------------------------------------------------
# Web chat (HTTP + SSE)
# ---------------------------------------------------------------------------


class _ChatSession:
    """The turn's own lookups: an active account, an unknown conversation, no companion PIN lock."""

    async def execute(self, _stmt):
        account = SimpleNamespace(is_active=True, password_changed_at=None)
        return SimpleNamespace(first=lambda: account, scalar_one_or_none=lambda: None)

    async def get(self, _model, _key):
        return None


def _web_chat_client(monkeypatch, account) -> TestClient:
    import app.api.v1.platform as platform_api
    from app.api.v1 import chat_web_stream
    from app.core.database import get_db

    async def _chat_enabled(_db):
        return True

    async def _db():
        yield _UserLookup(account)

    monkeypatch.setattr(platform_api, "is_chat_enabled", _chat_enabled)
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)
    app = FastAPI()
    app.include_router(chat_web_stream.router, prefix="/api/v1/web")
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def _web_chat_refusals(client: TestClient, token: str) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = str(uuid.uuid4())
    answers = [
        client.post("/api/v1/web/chat/messages", json={"content": "hello", "conversation_id": conversation_id}, headers=headers),
        client.post("/api/v1/web/chat/messages/steer", json={"content": "also", "conversation_id": conversation_id}, headers=headers),
        client.get(f"/api/v1/web/chat/conversations/{conversation_id}/events", headers=headers),
    ]
    assert {a.status_code for a in answers} == {401}
    return [a.json()["detail"] for a in answers]


def test_an_idle_session_cannot_send_steer_or_follow_on_the_web_chat(monkeypatch, redis):
    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    details = _web_chat_refusals(_web_chat_client(monkeypatch, account), token)
    assert {(d["code"], d["reason"]) for d in details} == {("session_expired", "idle")}


def test_a_session_past_its_lifetime_cannot_send_on_the_web_chat(monkeypatch, redis):
    account = _account()
    token = create_access_token(str(account.id), continues={"auth_time": _now() - 8 * 24 * 3600})
    details = _web_chat_refusals(_web_chat_client(monkeypatch, account), token)
    assert {(d["code"], d["reason"]) for d in details} == {("session_expired", "lifetime")}


def test_an_expired_confined_token_is_an_invalid_token_on_the_web_chat(monkeypatch, redis):
    details = _web_chat_refusals(_web_chat_client(monkeypatch, _account()), _expired_confined_token("type"))
    assert not any(isinstance(d, dict) and d.get("code") == "session_expired" for d in details)


async def _run_send(monkeypatch, token: str):
    """One web chat send on *token*'s session: the frames it writes, the runner to await, and its connection."""
    import app.core.database as database
    from app.api.v1 import chat as chat_module
    from app.services.chat_web_transport import HeadlessTurnConnection

    @asynccontextmanager
    async def _session():
        yield _ChatSession()

    monkeypatch.setattr(chat_module, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(database, "async_session", _session)
    claims = decode_access_token(token)
    web_session = await sessions.verify_session(claims, token)
    frames: list[dict] = []

    async def _written(message):
        body = (message.get("body") or b"").decode()
        frames.extend(json.loads(c[6:]) for c in body.split("\n\n") if c.startswith("data: "))

    conversation_id = str(uuid.uuid4())
    connection = HeadlessTurnConnection(conversation_id, {"content": "hello"})
    connection.bind(_written)
    auth = chat_module.ChatConnectionAuth(user_id=uuid.UUID(claims["sub"]), session=web_session)
    return frames, chat_module.run_chat_connection(connection, conversation_id, auth), connection


async def test_no_turn_starts_on_a_session_that_went_idle_after_its_send(monkeypatch, redis):
    _set(session_idle_timeout_minutes=30)
    token = create_access_token(str(uuid.uuid4()))
    frames, run, _connection = await _run_send(monkeypatch, token)
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    await asyncio.wait_for(run, 5)
    assert frames == [{"type": "session_expired", "reason": "idle"}]


async def test_an_active_session_runs_its_turn(monkeypatch, redis):
    _set(session_idle_timeout_minutes=30)
    token = create_access_token(str(uuid.uuid4()))
    frames, run, connection = await _run_send(monkeypatch, token)
    await asyncio.wait_for(run, 5)
    # Past the session check: the stubbed conversation lookup refuses next.
    assert frames == []
    assert (connection.refusal.status_code, connection.refusal.detail) == (404, "Conversation not found")


async def test_the_web_chat_events_stream_ends_with_an_event_when_its_session_goes_idle(monkeypatch, redis):
    from app.api.v1 import chat_web_stream
    from app.services import chat_turn_stream

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    web_session = await sessions.verify_session(decode_access_token(token), token)

    async def _follows(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _follows)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(chat_web_stream.chat_turn_guard, "mark_client_seen", AsyncMock())
    monkeypatch.setattr(chat_web_stream, "SESSION_RECHECK_SECONDS", 0)
    conversation = SimpleNamespace(id=uuid.uuid4(), user_id=account.id)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: conversation)))
    response = await chat_web_stream.stream_web_chat_events(
        str(conversation.id),
        SimpleNamespace(headers={}),
        client_id="tab-1",
        db=db,
        user=SimpleNamespace(id=account.id, web_session=web_session),
    )
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    frames = [frame async for frame in response.body_iterator]
    assert frames[-1] == b'data: {"type": "session_expired", "reason": "idle"}\n\n'


# ---------------------------------------------------------------------------
# Activity stream
# ---------------------------------------------------------------------------


class _QuietPubSub:
    async def subscribe(self, *_channels):
        return None

    async def unsubscribe(self, *_channels):
        return None

    async def get_message(self, **_kwargs):
        await asyncio.sleep(0)
        return None

    async def aclose(self):
        return None


class _PubSubClient:
    def pubsub(self):
        return _QuietPubSub()

    async def aclose(self):
        return None


async def test_the_activity_stream_ends_with_an_event_when_its_session_goes_idle(monkeypatch, redis):
    from app.api.v1 import activity as activity_module

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))

    async def _pubsub_client():
        return _PubSubClient()

    monkeypatch.setattr(activity_module, "redis_pubsub_client", _pubsub_client)
    monkeypatch.setattr(activity_module, "release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(activity_module, "SESSION_RECHECK_SECONDS", 0)
    monkeypatch.setattr(activity_module.asyncio, "sleep", AsyncMock())
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)

    response = await activity_module.activity_stream(request, token=token, db=_UserLookup(account))
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    frames = [frame async for frame in response.body_iterator]
    assert frames[-1] == f"event: session_expired\ndata: {json.dumps({'reason': 'idle'})}\n\n"


# ---------------------------------------------------------------------------
# MCP OAuth consent page
# ---------------------------------------------------------------------------


async def test_the_mcp_consent_page_refuses_an_idle_session(redis):
    from app.api.v1.mcp_oauth import _resolve_session_user

    _set(session_idle_timeout_minutes=30)
    account = _account()
    token = create_access_token(str(account.id))
    redis.store[_activity_key(token)] = str(_now() - 31 * 60)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})
    with pytest.raises(HTTPException) as exc:
        await _resolve_session_user(request, _UserLookup(account))
    assert (exc.value.status_code, exc.value.detail) == (401, "Session expired, please sign in again")


# ---------------------------------------------------------------------------
# The pure rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"auth_time": 100, "iat": 900}, 100),
        ({"iat": 900}, 900),
        ({"auth_time": True, "iat": 900}, 900),  # a bool is no time
        ({}, None),
    ],
)
def test_the_sign_in_time_is_auth_time_else_iat(claims, expected):
    assert sessions.session_started_at(claims) == expected


async def test_the_idle_deadline_is_read_off_the_last_activity(redis):
    _set(session_idle_timeout_minutes=10)
    claims = {"sub": "u", "auth_time": 1_000, "sid": "s", "exp": 1_000_000}
    redis.store[sessions.activity_key(claims, "t")] = "5000"
    alive = await sessions.verify_session(claims, "t", now=5000 + 599)
    assert (alive.idle_expires_at, alive.idle_timeout, alive.idle_enforced) == (5600, 600, True)
    with pytest.raises(sessions.SessionEndedError) as exc:
        await sessions.verify_session(claims, "t", now=5600)
    assert exc.value.reason == "idle"
    recorded = await sessions.record_activity(alive, now=5500)
    assert recorded.idle_expires_at == 6100
    assert await sessions.recheck_session(recorded, now=6099)


def test_the_401_names_the_reason():
    assert sessions.session_ended_detail("lifetime")["code"] == "session_expired"
