"""``POST /xtm/auth/exchange``: a registered platform's user token for an XTM One token.

OpenCTI or OpenAEV forwards the answer to another registered platform
(OpenCRQ reads it as ``X-XTM-User-Token``), which trusts XTM One as the single
issuer of the user's identity. The caller's token is verified as at platform
sign-in; the user is the one it names; the audience is another active
registration, never the caller nor XTM One.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import xtm_auth as route
from app.core.config import settings
from app.core.database import get_db
from app.core.xtm_auth import _kid, _private_key, _public_key_raw

XTM_ONE = "http://localhost:8090"
OPENCTI = "http://localhost:8080"
OPENAEV = "http://localhost:8081"
OPENCRQ = "http://localhost:8082"
EMAIL = "analyst@example.com"


@pytest.fixture(autouse=True)
def _identity_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "base_url", XTM_ONE)
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])

    class _Client:
        def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda _url, **_kwargs: _Client())

    async def _no_limit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(route, "enforce_rate_limit", _no_limit)


def _registration(platform_url: str, identifier: str) -> SimpleNamespace:
    return SimpleNamespace(platform_identifier=identifier, platform_url=platform_url, api_url=None)


ACTIVE = (
    _registration(OPENCTI, "opencti"),
    _registration(OPENAEV, "openaev"),
    _registration(OPENCRQ, "opencrq"),
)


class _Result:
    def __init__(self, rows: list[Any], scalar: object = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> object:
        return self._scalar


class _Db:
    """The ACTIVE registrations (the status filter is the query's) and one user row."""

    def __init__(self, registrations: Iterable[SimpleNamespace] = ACTIVE, user: object = None) -> None:
        self.registrations = list(registrations)
        self.user = user

    async def execute(self, statement: object) -> _Result:
        if "platform_registrations" in str(statement):
            return _Result(self.registrations)
        return _Result([], self.user)


def _user(active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email=EMAIL, is_active=active)


def _token(*, iss: str = OPENCTI, aud: str = XTM_ONE, email: str = EMAIL, lifetime: int = 300, age: int = 0) -> str:
    now = datetime.now(UTC) - timedelta(seconds=age)
    payload = {
        "iss": iss,
        "sub": "opencti-user-1",
        "email": email,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(seconds=lifetime),
        "jti": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


def _client(db: _Db) -> TestClient:
    app = FastAPI()
    app.include_router(route.router, prefix="/xtm/auth")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _exchange(db: _Db, token: str | None, audience: str = OPENCRQ) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response: httpx.Response = _client(db).post("/xtm/auth/exchange", json={"audience": audience}, headers=headers)
    return response


def test_a_member_gets_an_xtm_one_token_for_the_target_platform() -> None:
    user = _user()
    response = _exchange(_Db(user=user), _token())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["audience"] == OPENCRQ
    header = pyjwt.get_unverified_header(body["access_token"])
    assert header == {"alg": "EdDSA", "kid": _kid, "typ": "JWT"}
    claims = pyjwt.decode(
        body["access_token"],
        Ed25519PublicKey.from_public_bytes(_public_key_raw),
        algorithms=["EdDSA"],
        audience=OPENCRQ,
        issuer=XTM_ONE,
    )
    # The XTM One subject, never the platform's own user id.
    assert claims["sub"] == str(user.id)
    assert claims["email"] == EMAIL
    assert claims["exp"] - claims["iat"] <= 600


def test_openaev_may_exchange_too() -> None:
    assert _exchange(_Db(user=_user()), _token(iss=OPENAEV)).status_code == 200


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        pyjwt.encode({"sub": "x"}, "a-local-secret-of-32-bytes-minimum!", algorithm="HS256"),
        "fcp-an-api-key",
    ],
)
def test_anything_but_a_platform_user_token_is_refused(token: str | None) -> None:
    response = _exchange(_Db(user=_user()), token)
    assert response.status_code == 401


def test_a_suspended_or_revoked_registration_is_refused() -> None:
    """The registration of the caller is not ACTIVE: the issuer is not trusted."""
    registrations = [r for r in ACTIVE if r.platform_identifier != "opencti"]
    assert _exchange(_Db(registrations, user=_user()), _token()).status_code == 401


def test_an_expired_token_is_refused() -> None:
    assert _exchange(_Db(user=_user()), _token(lifetime=300, age=900)).status_code == 401


def test_a_token_addressed_to_another_platform_is_refused() -> None:
    assert _exchange(_Db(user=_user()), _token(aud=OPENCRQ)).status_code == 401


@pytest.mark.parametrize(
    "audience",
    [
        "http://localhost:9999",  # no registration
        "https://evil.example",
        OPENCTI,  # the caller itself
        XTM_ONE,  # XTM One
        "not a url",
    ],
)
def test_an_audience_that_is_not_another_active_registration_is_refused(audience: str) -> None:
    assert _exchange(_Db(user=_user()), _token(), audience=audience).status_code == 403


def test_a_suspended_target_registration_is_refused() -> None:
    registrations = [r for r in ACTIVE if r.platform_identifier != "opencrq"]
    assert _exchange(_Db(registrations, user=_user()), _token()).status_code == 403


def test_xtm_one_registered_as_a_platform_is_still_never_an_audience() -> None:
    registrations = [*ACTIVE, _registration(XTM_ONE, "loop")]
    assert _exchange(_Db(registrations, user=_user()), _token(), audience=XTM_ONE).status_code == 403


def test_an_unknown_user_is_refused_and_never_provisioned() -> None:
    db = _Db(user=None)
    response = _exchange(db, _token())
    assert response.status_code == 403
    assert not hasattr(db, "added")


def test_a_deactivated_user_is_refused() -> None:
    assert _exchange(_Db(user=_user(active=False)), _token()).status_code == 403


def test_the_subject_is_the_one_the_caller_signed_never_one_it_names() -> None:
    """The body carries the audience alone: a subject or email it names is not read."""
    user = _user()
    response = _client(_Db(user=user)).post(
        "/xtm/auth/exchange",
        json={"audience": OPENCRQ, "sub": "someone-else", "email": "ceo@example.com"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 200
    claims = pyjwt.decode(response.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == str(user.id)
    assert claims["email"] == EMAIL


def test_the_token_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    response = _exchange(_Db(user=_user()), _token())
    issued = response.json()["access_token"]
    assert issued not in caplog.text
    assert "XTM user token exchanged" in caplog.text


def test_the_platform_serves_the_exchange() -> None:
    from app.main import application

    routes = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()) or ()))) for r in application.routes}
    assert ("/xtm/auth/exchange", ("POST",)) in routes
