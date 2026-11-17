"""The activity stream authenticates a session token under the REST rules.

An ``EventSource`` sends no ``Authorization`` header, so ``/activity/stream``
reads its token from the query string and cannot depend on
``deps.get_current_user``. It re-implemented the checks and dropped one: a
token issued before the user's last password change, which REST refuses, kept
streaming the account's activity feed. It now resolves the token through
``deps.resolve_session_jwt``, the resolver the REST dependency uses, so every
outcome below is REST's own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api import deps
from app.api.v1 import activity as activity_module

# Within the session lifetime: a token issued then is still a session.
_CHANGED = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
_CHANGED_TS = int(_CHANGED.timestamp())


def _db_answering(account) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = account
    db.execute = AsyncMock(return_value=result)
    return db


def _account(*, is_active: bool = True, password_changed_at: datetime | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(), is_active=is_active, password_changed_at=password_changed_at
    )


def _open_stream(monkeypatch, payload, account):
    """Call the real route with *payload* as the decoded token, up to the stream."""
    monkeypatch.setattr(deps, "decode_access_token", lambda _t: payload)
    monkeypatch.setattr(
        activity_module, "release_db_connection_before_loop", AsyncMock()
    )
    request = MagicMock()
    return activity_module.activity_stream(request, token="tok", db=_db_answering(account))


@pytest.mark.parametrize(
    ("payload_extra", "account", "detail"),
    [
        ({"iat": _CHANGED_TS - 1}, _account(password_changed_at=_CHANGED), "Session expired, please sign in again"),
        ({}, _account(password_changed_at=_CHANGED), "Session expired, please sign in again"),
        ({"iat": _CHANGED_TS}, _account(is_active=False), "User not found"),
        ({"iat": _CHANGED_TS}, None, "User not found"),
        ({"iat": _CHANGED_TS, "type": "a2a_partner"}, _account(), "Invalid token"),
        ({"iat": _CHANGED_TS, "scope": "mfa"}, _account(), "Invalid token"),
    ],
    ids=[
        "issued-before-the-password-change",
        "no-iat-after-a-password-change",
        "deactivated",
        "account-gone",
        "confined-type",
        "confined-scope",
    ],
)
async def test_the_stream_refuses_what_rest_refuses(monkeypatch, payload_extra, account, detail):
    payload = {"sub": str(uuid.uuid4()), **payload_extra}

    with pytest.raises(HTTPException) as stream_exc:
        await _open_stream(monkeypatch, payload, account)
    with pytest.raises(HTTPException) as rest_exc:
        await deps.get_current_user_ungated(
            credentials=SimpleNamespace(credentials="tok"), db=_db_answering(account)
        )

    assert stream_exc.value.status_code == rest_exc.value.status_code == 401
    assert stream_exc.value.detail == rest_exc.value.detail == detail


async def test_an_undecodable_token_is_refused_like_rest(monkeypatch):
    def _expired(_token):
        raise ValueError("Signature has expired")

    monkeypatch.setattr(deps, "decode_access_token", _expired)

    with pytest.raises(HTTPException) as exc:
        await activity_module.activity_stream(MagicMock(), token="tok", db=_db_answering(None))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


async def test_a_token_issued_at_the_password_change_still_streams(monkeypatch):
    account = _account(password_changed_at=_CHANGED)

    response = await _open_stream(
        monkeypatch, {"sub": str(account.id), "iat": _CHANGED_TS}, account
    )

    assert response.media_type == "text/event-stream"
