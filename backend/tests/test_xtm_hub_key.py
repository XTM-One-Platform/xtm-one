"""The XTM Hub signing key: ``app.core.xtm_hub_key``."""

import base64
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest

from app.core import xtm_auth, xtm_hub_key
from app.core.config import settings
from app.core.security import PUBLISHED_SECRET_KEYS
from app.core.xtm_hub_key import (
    ASSERTION_AUDIENCE,
    PLATFORM_IDENTIFIER,
    XtmHubKeyUnavailableError,
    derive_key,
    instance_key,
    key_version,
)
from app.models.platform_settings import PlatformSettings

_SECRET = "x" * 24 + "-a-strong-test-secret-key"
_PLATFORM_ID = uuid.UUID("6f1c9a8e-4b1f-4d3a-9c55-0e2f7a1b3c4d")


@pytest.fixture(autouse=True)
def _strong_secret(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", _SECRET)


def _verify(token: str, jwk: dict, **options) -> dict:
    """Verify *token* the way the Hub does: the stored public JWK, EdDSA only."""
    return pyjwt.decode(
        token,
        pyjwt.PyJWK(jwk).key,
        algorithms=["EdDSA"],
        audience=ASSERTION_AUDIENCE,
        issuer=str(_PLATFORM_ID),
        options={"require": ["iss", "aud", "iat", "exp", "jti"]},
        **options,
    )


class TestDerivation:
    def test_the_same_version_gives_the_same_key(self):
        first, second = derive_key(1), derive_key(1)
        assert first.public_jwk() == second.public_jwk()
        assert first.kid == second.kid

    def test_the_next_version_gives_another_key(self):
        v1, v2 = derive_key(1), derive_key(2)
        assert v1.public_key_raw != v2.public_key_raw
        assert v1.kid != v2.kid

    def test_another_secret_gives_another_key(self, monkeypatch):
        before = derive_key(1)
        monkeypatch.setattr(settings, "secret_key", _SECRET + "-rotated")
        assert derive_key(1).public_key_raw != before.public_key_raw

    def test_the_key_is_not_the_xtm_auth_key(self):
        # Both derived from the same (patched) SECRET_KEY: only the labels differ.
        _, _, xtm_auth_public_key_raw = xtm_auth._derive_ed25519_keypair()
        assert all(derive_key(version).public_key_raw != xtm_auth_public_key_raw for version in (1, 2))

    @pytest.mark.parametrize("version", [0, -1, 1.0, "1", True, None])
    def test_a_version_that_is_not_a_positive_integer_is_refused(self, version):
        with pytest.raises(ValueError):
            derive_key(version)

    @pytest.mark.parametrize("secret", ["", "short", *sorted(PUBLISHED_SECRET_KEYS)])
    def test_no_key_while_the_secret_key_is_guessable(self, monkeypatch, secret):
        monkeypatch.setattr(settings, "secret_key", secret)
        with pytest.raises(XtmHubKeyUnavailableError, match="SECRET_KEY is guessable"):
            derive_key(1)


class TestPublicJwk:
    def test_the_jwk_carries_the_public_key_only(self):
        jwk = derive_key(1).public_jwk()
        assert set(jwk) == {"kty", "crv", "x", "kid", "alg", "use"}
        assert (jwk["kty"], jwk["crv"], jwk["alg"], jwk["use"]) == ("OKP", "Ed25519", "EdDSA", "sig")
        assert "d" not in jwk

    def test_x_is_the_32_byte_public_key_in_base64url(self):
        key = derive_key(1)
        x = key.public_jwk()["x"]
        assert "=" not in x
        assert base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)) == key.public_key_raw
        assert len(key.public_key_raw) == 32

    def test_the_kid_is_the_rfc_7638_thumbprint(self):
        jwk = derive_key(1).public_jwk()
        assert jwk["kid"] == _rfc7638(jwk)

    def test_the_private_key_never_shows_in_the_repr(self):
        assert "private" not in repr(derive_key(1))


def _rfc7638(jwk: dict) -> str:
    canonical = json.dumps({k: jwk[k] for k in ("crv", "kty", "x")}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()


class TestAssertion:
    def test_a_signed_assertion_verifies_with_the_public_jwk(self):
        key = derive_key(1)
        claims = _verify(key.sign_assertion(_PLATFORM_ID), key.public_jwk())
        assert claims["iss"] == str(_PLATFORM_ID)
        assert claims["aud"] == ASSERTION_AUDIENCE
        assert claims["identifier"] == PLATFORM_IDENTIFIER

    def test_the_header_names_the_key(self):
        key = derive_key(1)
        header = pyjwt.get_unverified_header(key.sign_assertion(_PLATFORM_ID))
        assert header == {"alg": "EdDSA", "kid": key.kid, "typ": "JWT"}

    def test_it_lives_five_minutes(self):
        key = derive_key(1)
        claims = _verify(key.sign_assertion(_PLATFORM_ID), key.public_jwk())
        assert claims["exp"] - claims["iat"] == 300

    def test_every_assertion_has_its_own_jti(self):
        key = derive_key(1)
        jtis = {_verify(key.sign_assertion(_PLATFORM_ID), key.public_jwk())["jti"] for _ in range(5)}
        assert len(jtis) == 5

    def test_an_assertion_does_not_verify_with_another_version(self):
        token = derive_key(1).sign_assertion(_PLATFORM_ID)
        with pytest.raises(pyjwt.InvalidSignatureError):
            _verify(token, derive_key(2).public_jwk())

    def test_the_platform_id_may_be_a_string(self):
        key = derive_key(1)
        assert _verify(key.sign_assertion(str(_PLATFORM_ID)), key.public_jwk())["iss"] == str(_PLATFORM_ID)


class TestInstanceSetting:
    def test_the_setting_starts_at_one(self):
        column = PlatformSettings.__table__.columns["xtm_hub_key_version"]
        assert not column.nullable
        assert column.server_default.arg == "1"
        assert column.default.arg == 1

    def test_the_database_refuses_a_version_below_one(self):
        checks = {
            c.name: str(c.sqltext)
            for c in PlatformSettings.__table__.constraints
            if c.name == "ck_platform_settings_xtm_hub_key_version_positive"
        }
        assert checks == {"ck_platform_settings_xtm_hub_key_version_positive": "xtm_hub_key_version >= 1"}

    async def test_the_key_follows_the_stored_version(self):
        db = SimpleNamespace(scalar=AsyncMock(return_value=3))
        assert await key_version(db) == 3
        assert (await instance_key(db)).public_jwk() == derive_key(3).public_jwk()

    async def test_version_one_before_the_settings_row_exists(self):
        db = SimpleNamespace(scalar=AsyncMock(return_value=None))
        assert await key_version(db) == xtm_hub_key.DEFAULT_KEY_VERSION == 1
