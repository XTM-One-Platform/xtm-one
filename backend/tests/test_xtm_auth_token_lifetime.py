"""A cross-platform token must say when it dies, and die soon.

PyJWT only checks ``exp`` when the claim is present: without ``require``, a
token a registered platform signed with no ``exp`` was accepted and never
expired. OpenCTI, OpenAEV and OpenCRQ all sign ``iat`` and ``exp`` for 10
minutes, in every released version, so the verifier requires both and refuses
a token signed to live longer than :data:`XTM_JWT_MAX_LIFETIME`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import settings
from app.core.xtm_auth import (
    XTM_JWT_MAX_LIFETIME,
    _kid,
    _private_key,
    _public_key_raw,
    issue_xtm_jwt,
    verify_xtm_jwt,
)

PLATFORM = "http://localhost:8080"
XTM_ONE = "http://localhost:8090"


@pytest.fixture(autouse=True)
def _identity_and_keys(monkeypatch):
    monkeypatch.setattr(settings, "base_url", XTM_ONE)
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])

    class _Client:
        def get_signing_key_from_jwt(self, _token: str):
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda _url, **_kwargs: _Client())


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _Db:
    """A session holding one active registration of *platform_url*."""

    def __init__(self, platform_url: str = PLATFORM) -> None:
        self.row = SimpleNamespace(platform_identifier="opencti", platform_url=platform_url, api_url=None)

    async def execute(self, _statement):
        return _Result([self.row])


def _token(*, omit: tuple[str, ...] = (), iat_ago: timedelta = timedelta(0), lifetime: timedelta | None = None) -> str:
    """A token as the platforms sign it: ``iat`` now, 10 minutes of life."""
    iat = datetime.now(UTC) - iat_ago
    payload = {
        "iss": PLATFORM,
        "sub": str(uuid.uuid4()),
        "email": "analyst@example.com",
        "aud": XTM_ONE,
        "iat": iat,
        "nbf": iat,
        "exp": iat + (timedelta(minutes=10) if lifetime is None else lifetime),
        "jti": str(uuid.uuid4()),
    }
    for claim in omit:
        payload.pop(claim)
    return pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


async def test_a_token_as_the_platforms_sign_it_is_accepted():
    payload = await verify_xtm_jwt(_token(), _Db())
    assert payload is not None
    assert payload.email == "analyst@example.com"


async def test_a_token_without_an_expiry_is_refused(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        assert await verify_xtm_jwt(_token(omit=("exp",)), _Db()) is None
    assert 'missing the "exp" claim' in caplog.text


async def test_a_token_without_an_issue_time_is_refused(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        assert await verify_xtm_jwt(_token(omit=("iat",)), _Db()) is None
    assert 'missing the "iat" claim' in caplog.text


async def test_an_expired_token_is_still_refused(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        assert await verify_xtm_jwt(_token(iat_ago=timedelta(minutes=20)), _Db()) is None
    assert "token expired" in caplog.text


async def test_a_token_at_the_maximum_lifetime_is_accepted():
    assert await verify_xtm_jwt(_token(lifetime=XTM_JWT_MAX_LIFETIME), _Db()) is not None


@pytest.mark.parametrize("lifetime", [XTM_JWT_MAX_LIFETIME + timedelta(seconds=1), timedelta(days=36500)])
async def test_a_token_signed_to_live_longer_is_refused(caplog, lifetime):
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        assert await verify_xtm_jwt(_token(lifetime=lifetime), _Db()) is None
    assert "exceeds the 900s maximum" in caplog.text


async def test_an_issue_time_in_the_future_cannot_stretch_the_lifetime():
    """The bound is only a bound because a future ``iat`` is refused first."""
    token = _token(iat_ago=-timedelta(days=365), lifetime=timedelta(minutes=10))
    assert await verify_xtm_jwt(token, _Db()) is None


async def test_the_tokens_xtm_one_signs_meet_the_contract_it_enforces():
    """XTM One's own 10-minute tokens pass the checks it applies to the platforms'."""
    token = issue_xtm_jwt(uuid.uuid4(), "analyst@example.com", XTM_ONE)
    assert await verify_xtm_jwt(token, _Db(platform_url=XTM_ONE)) is not None
