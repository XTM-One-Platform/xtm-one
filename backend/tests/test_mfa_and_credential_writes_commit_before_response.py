"""Two-factor and credential writes commit before they answer.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the
response is sent. The self-service two-factor routes and the API / MCP key
routes write security state - a consumed TOTP step or recovery code, a secret,
a key issued or revoked - that must be on record before the caller is told the
outcome: otherwise another connection still reads the previous state until the
teardown commit lands, and a failing teardown commit loses the write after the
session or the key was already handed out.

Each route is driven through a session stub that records its calls in order.
The commit has to be the last of them, and every two-factor route must re-read
the user under a row lock before it checks its preconditions or a one-time
code: a precondition read before the lock (MFA enabled or not) can be stale by
the time the route writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import HTTPException

from app.api.v1 import api_keys as api_keys_module
from app.api.v1 import auth as auth_module
from app.api.v1 import settings as settings_module
from app.services import mfa


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RecordingSession:
    """Just enough ``AsyncSession`` for these routes, in call order."""

    def __init__(self, row=None):
        self.calls: list[str] = []
        self._row = row

    async def execute(self, _stmt):
        self.calls.append("execute")
        return _Result(self._row)

    async def get(self, _model, _pk):
        self.calls.append("get")
        return self._row

    async def refresh(self, _obj, with_for_update=None):
        self.calls.append("lock" if with_for_update else "refresh")

    def add(self, obj):
        self.calls.append("add")
        # The column defaults a real flush would apply.
        defaults = {
            "id": uuid.uuid4(),
            "created_at": datetime.now(UTC),
            "is_active": True,
            "is_from_env": False,
        }
        for name, value in defaults.items():
            if getattr(obj, name, None) is None:
                setattr(obj, name, value)

    async def flush(self):
        self.calls.append("flush")

    async def delete(self, _obj):
        self.calls.append("delete")

    async def commit(self):
        self.calls.append("commit")


async def _record_audit(db, **_kw) -> None:
    db.calls.append("audit")


async def _noop(*_a, **_kw) -> None:
    return None


def _assert_committed_last(session: _RecordingSession, *before: str) -> None:
    calls = session.calls
    assert calls.count("commit") == 1, f"expected exactly one commit: {calls}"
    assert calls[-1] == "commit", (
        "the commit must be the route's last session call, so the answer only "
        f"ever describes committed state: {calls}"
    )
    for call in before:
        assert call in calls, f"expected {call!r} before the commit: {calls}"


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


def _code(secret: str) -> str:
    return pyotp.TOTP(
        secret, digits=mfa.TOTP_DIGITS, interval=mfa.TOTP_PERIOD_SECONDS
    ).generate_otp(mfa.current_step())


def _user(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "email": "u@example.com",
        "full_name": "Test User",
        "role": "user",
        "is_active": True,
        "hashed_password": None,
        "password_changed_at": None,
        "sso_provider": None,
        "mfa_enabled": False,
        "mfa_secret": None,
        "mfa_confirmed_at": None,
        "mfa_last_used_step": None,
        "mfa_recovery_codes": None,
    }
    return SimpleNamespace(**{**fields, **overrides})


@pytest.fixture(autouse=True)
def _quiet_auth(monkeypatch):
    monkeypatch.setattr(auth_module, "log_audit", _record_audit)
    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop)


# -- /auth/mfa/* ----------------------------------------------------------------


async def test_mfa_setup_commits_before_returning_the_secret():
    user = _user()
    session = _RecordingSession()

    response = await auth_module.mfa_setup(request=_request(), user=user, db=session)

    _assert_committed_last(session, "lock", "flush")
    assert user.mfa_secret and response.secret


async def test_mfa_enable_commits_before_returning_the_recovery_codes():
    secret = mfa.generate_secret()
    user = _user(mfa_secret=mfa.encrypt_secret(secret))
    session = _RecordingSession()

    response = await auth_module.mfa_enable(
        body=auth_module.MfaCodeRequest(code=_code(secret)),
        request=_request(),
        user=user,
        db=session,
    )

    _assert_committed_last(session, "lock", "flush", "audit")
    assert user.mfa_enabled is True
    assert response.recovery_codes


async def test_mfa_verify_commits_before_issuing_the_session(monkeypatch):
    # The clock the TOTP check reads is frozen: read twice from the wall clock,
    # the code's step and the asserted step differ when a 30 s step rolls over
    # during the call.
    frozen_now = mfa.current_step() * mfa.TOTP_PERIOD_SECONDS + 1
    monkeypatch.setattr(mfa, "time", SimpleNamespace(time=lambda: frozen_now))
    step = mfa.current_step()
    secret = mfa.generate_secret()
    user = _user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    session = _RecordingSession(row=user)

    response = await auth_module.mfa_verify(
        body=auth_module.MfaVerifyRequest(
            mfa_token=mfa.create_challenge_token(user.id), code=_code(secret)
        ),
        request=_request(),
        db=session,
    )

    _assert_committed_last(session, "lock", "flush", "audit")
    assert session.calls.index("lock") < session.calls.index("audit")
    assert user.mfa_last_used_step == step
    assert response.access_token


async def test_mfa_verify_commits_a_consumed_recovery_code_before_issuing_the_session():
    plaintext, stored = mfa.generate_recovery_codes()
    user = _user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(mfa.generate_secret()),
        mfa_recovery_codes=stored,
    )
    session = _RecordingSession(row=user)

    response = await auth_module.mfa_verify(
        body=auth_module.MfaVerifyRequest(
            mfa_token=mfa.create_challenge_token(user.id), code=plaintext[0]
        ),
        request=_request(),
        db=session,
    )

    _assert_committed_last(session, "lock", "flush", "audit")
    assert mfa.count_unused_recovery_codes(user.mfa_recovery_codes) == (
        mfa.RECOVERY_CODE_COUNT - 1
    )
    assert response.access_token


async def test_mfa_disable_commits_before_answering():
    secret = mfa.generate_secret()
    user = _user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    session = _RecordingSession()

    await auth_module.mfa_disable(
        body=auth_module.MfaDisableRequest(code=_code(secret)),
        request=_request(),
        user=user,
        db=session,
    )

    _assert_committed_last(session, "lock", "flush", "audit")
    assert user.mfa_enabled is False


async def test_mfa_recovery_codes_commit_before_the_new_set_is_shown():
    secret = mfa.generate_secret()
    _old_plaintext, old_stored = mfa.generate_recovery_codes()
    user = _user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
        mfa_recovery_codes=old_stored,
    )
    session = _RecordingSession()

    response = await auth_module.mfa_regenerate_recovery_codes(
        body=auth_module.MfaCodeRequest(code=_code(secret)),
        request=_request(),
        user=user,
        db=session,
    )

    _assert_committed_last(session, "lock", "flush", "audit")
    assert user.mfa_recovery_codes != old_stored
    assert len(response.recovery_codes) == mfa.RECOVERY_CODE_COUNT


# -- preconditions are read under the lock --------------------------------------


class _ConcurrentlyChangedSession(_RecordingSession):
    """The locked re-read returns what another request committed meanwhile."""

    def __init__(self, user, **committed):
        super().__init__(row=user)
        self._user = user
        self._committed = committed

    async def refresh(self, obj, with_for_update=None):
        await super().refresh(obj, with_for_update=with_for_update)
        for name, value in self._committed.items():
            setattr(self._user, name, value)


async def test_mfa_enable_refuses_when_a_concurrent_enable_committed_first():
    secret = mfa.generate_secret()
    user = _user(mfa_secret=mfa.encrypt_secret(secret))
    first_codes = ["hash-of-the-set-the-user-was-shown"]
    session = _ConcurrentlyChangedSession(
        user, mfa_enabled=True, mfa_recovery_codes=first_codes
    )

    with pytest.raises(HTTPException) as exc:
        await auth_module.mfa_enable(
            body=auth_module.MfaCodeRequest(code=_code(secret)),
            request=_request(),
            user=user,
            db=session,
        )

    assert exc.value.status_code == 409
    assert user.mfa_recovery_codes is first_codes
    assert "commit" not in session.calls


async def test_mfa_setup_refuses_when_a_concurrent_enable_committed_first():
    active_secret = mfa.encrypt_secret(mfa.generate_secret())
    user = _user()
    session = _ConcurrentlyChangedSession(
        user, mfa_enabled=True, mfa_secret=active_secret
    )

    with pytest.raises(HTTPException) as exc:
        await auth_module.mfa_setup(request=_request(), user=user, db=session)

    assert exc.value.status_code == 409
    assert user.mfa_secret == active_secret


@pytest.mark.parametrize("route", ["disable", "recovery-codes"])
async def test_mfa_routes_refuse_when_a_concurrent_disable_committed_first(route):
    secret = mfa.generate_secret()
    user = _user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    session = _ConcurrentlyChangedSession(user, mfa_enabled=False, mfa_secret=None)

    with pytest.raises(HTTPException) as exc:
        if route == "disable":
            await auth_module.mfa_disable(
                body=auth_module.MfaDisableRequest(code=_code(secret)),
                request=_request(),
                user=user,
                db=session,
            )
        else:
            await auth_module.mfa_regenerate_recovery_codes(
                body=auth_module.MfaCodeRequest(code=_code(secret)),
                request=_request(),
                user=user,
                db=session,
            )

    assert exc.value.status_code == 409
    assert "commit" not in session.calls


# -- API keys and the global MCP key ------------------------------------------


async def test_create_api_key_commits_before_returning_the_key(monkeypatch):
    async def _fast_hash(raw_key: str) -> str:
        return f"hashed:{raw_key}"

    monkeypatch.setattr(api_keys_module, "log_audit", _record_audit)
    monkeypatch.setattr(api_keys_module, "_hash_key", _fast_hash)
    session = _RecordingSession()

    response = await api_keys_module.create_api_key(
        body=api_keys_module.CreateApiKeyRequest(name="CI key"),
        request=_request(),
        user=_user(),
        db=session,
    )

    _assert_committed_last(session, "add", "flush", "audit")
    assert response.key


async def test_delete_api_key_commits_before_answering(monkeypatch):
    monkeypatch.setattr(api_keys_module, "log_audit", _record_audit)
    key = SimpleNamespace(id=uuid.uuid4(), name="CI key", is_from_env=False)
    session = _RecordingSession(row=key)

    await api_keys_module.delete_api_key(
        key_id=str(key.id), request=_request(), user=_user(), db=session
    )

    _assert_committed_last(session, "audit", "delete")


@pytest.fixture
def platform_settings(monkeypatch):
    import app.api.v1.platform_settings_helpers as helpers

    row = SimpleNamespace(mcp_global_api_key_hash=None, mcp_global_api_key_prefix=None)

    async def _load(_db):
        return row

    monkeypatch.setattr(helpers, "get_platform_settings", _load)
    monkeypatch.setattr(settings_module, "log_audit", _record_audit)
    return row


async def test_generate_mcp_key_commits_before_returning_the_key(platform_settings):
    session = _RecordingSession()

    response = await settings_module.generate_mcp_key(
        request=_request(), user=_user(role="admin"), db=session, _ai_gate=None
    )

    _assert_committed_last(session, "flush", "audit")
    assert response["key"].startswith("mcp-")
    assert platform_settings.mcp_global_api_key_hash


async def test_revoke_mcp_key_commits_before_answering(platform_settings):
    platform_settings.mcp_global_api_key_hash = "hash"
    platform_settings.mcp_global_api_key_prefix = "mcp-1234..."
    session = _RecordingSession()

    await settings_module.revoke_mcp_key(
        request=_request(), user=_user(role="admin"), db=session
    )

    _assert_committed_last(session, "flush", "audit")
    assert platform_settings.mcp_global_api_key_hash is None
