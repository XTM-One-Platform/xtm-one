"""Pytest coverage for app.core.xtm_auth — uses the real production module."""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.config import settings
from app.core.xtm_auth import (
    CrossJWTPayload,
    _get_jwk_client,
    _hkdf_derive,
    _kid,
    _normalize_url,
    _private_key,
    _public_key_raw,
    get_jwks_payload,
    issue_xtm_jwt,
    verify_xtm_jwt,
)

_BASE_URL = _normalize_url(settings.base_url)


# ---------------------------------------------------------------------------
# get_jwks_payload
# ---------------------------------------------------------------------------


class TestGetJwksPayload:
    def test_returns_valid_jwks_structure(self):
        jwks = get_jwks_payload()
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1

    def test_key_fields(self):
        key = get_jwks_payload()["keys"][0]
        assert key["kty"] == "OKP"
        assert key["crv"] == "Ed25519"
        assert key["use"] == "sig"
        assert key["alg"] == "EdDSA"
        assert key["kid"] == _kid
        x_padded = key["x"] + "=" * (-len(key["x"]) % 4)
        raw = base64.urlsafe_b64decode(x_padded)
        assert raw == _public_key_raw

    def test_deterministic(self):
        assert get_jwks_payload() == get_jwks_payload()


# ---------------------------------------------------------------------------
# Key derivation sanity
# ---------------------------------------------------------------------------


class TestKeyDerivation:
    def test_kid_is_16_hex_chars(self):
        assert len(_kid) == 16
        int(_kid, 16)

    def test_public_key_is_32_bytes(self):
        assert len(_public_key_raw) == 32

    def test_private_key_matches_public(self):
        pub = _private_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )
        assert pub == _public_key_raw

    def test_different_secret_yields_different_key(self):
        seed_a = _hkdf_derive(b"secret-a", b"xtm:jwt:ed25519:seed::v1")
        seed_b = _hkdf_derive(b"secret-b", b"xtm:jwt:ed25519:seed::v1")
        assert seed_a != seed_b


# ---------------------------------------------------------------------------
# issue_xtm_jwt
# ---------------------------------------------------------------------------


class TestIssueXtmJwt:
    def test_round_trip_decode(self):
        user_id = uuid.uuid4()
        token = issue_xtm_jwt(user_id, "alice@example.com", "https://target.test")
        decoded = pyjwt.decode(
            token, _private_key.public_key(), algorithms=["EdDSA"],
            audience="https://target.test",
        )
        assert decoded["sub"] == str(user_id)
        assert decoded["email"] == "alice@example.com"
        assert decoded["aud"] == "https://target.test"
        assert "jti" in decoded

    def test_header_contains_kid_and_eddsa(self):
        token = issue_xtm_jwt(uuid.uuid4(), "a@b.com", "https://target.test")
        header = pyjwt.get_unverified_header(token)
        assert header["kid"] == _kid
        assert header["alg"] == "EdDSA"

    def test_audience_always_present(self):
        token = issue_xtm_jwt(uuid.uuid4(), "a@b.com", "https://target.test")
        decoded = pyjwt.decode(
            token, _private_key.public_key(), algorithms=["EdDSA"],
            audience="https://target.test",
        )
        assert decoded["aud"] == "https://target.test"

    def test_audience_passed_through_as_is(self):
        """External audience URL is not normalized — used exactly as provided."""
        token = issue_xtm_jwt(uuid.uuid4(), "a@b.com", "https://target.test/")
        decoded = pyjwt.decode(
            token, _private_key.public_key(), algorithms=["EdDSA"],
            audience="https://target.test/",
        )
        assert decoded["aud"] == "https://target.test/"

    def test_wrong_key_rejects(self):
        token = issue_xtm_jwt(uuid.uuid4(), "a@b.com", "https://target.test")
        wrong_seed = _hkdf_derive(b"wrong-secret", b"xtm:jwt:ed25519:seed::v1")
        wrong_key = Ed25519PrivateKey.from_private_bytes(wrong_seed)
        with pytest.raises(pyjwt.InvalidSignatureError):
            pyjwt.decode(token, wrong_key.public_key(), algorithms=["EdDSA"],
                         audience="https://target.test")

    def test_expired_token_rejected(self):
        now = datetime.now(UTC)
        payload = {
            "iss": _BASE_URL, "sub": str(uuid.uuid4()), "email": "a@b.com",
            "aud": "https://target.test",
            "iat": now - timedelta(minutes=20), "exp": now - timedelta(minutes=10),
            "jti": str(uuid.uuid4()),
        }
        token = pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})
        with pytest.raises(pyjwt.ExpiredSignatureError):
            pyjwt.decode(token, _private_key.public_key(), algorithms=["EdDSA"],
                         audience="https://target.test")


# ---------------------------------------------------------------------------
# _get_jwk_client
# ---------------------------------------------------------------------------


class TestGetJwkClient:
    def test_caches_client_per_issuer(self):
        c1 = _get_jwk_client("https://issuer.test")
        c2 = _get_jwk_client("https://issuer.test")
        assert c1 is c2

    def test_different_issuers_get_different_clients(self):
        c1 = _get_jwk_client("https://a.test")
        c2 = _get_jwk_client("https://b.test")
        assert c1 is not c2

    def test_a_trailing_slash_fetches_the_published_route(self):
        c1 = _get_jwk_client("https://norm2.test/")
        c2 = _get_jwk_client("https://norm2.test")
        assert c1 is c2
        assert c1.uri == "https://norm2.test/xtm/auth/jwks"


# ---------------------------------------------------------------------------
# _normalize_url
# ---------------------------------------------------------------------------


class TestNormalizeUrl:
    def test_strips_trailing_slash(self):
        assert _normalize_url("https://example.com/") == "https://example.com"

    def test_no_op_without_slash(self):
        assert _normalize_url("https://example.com") == "https://example.com"

    def test_multiple_slashes(self):
        assert _normalize_url("https://example.com///") == "https://example.com"

    def test_lowercases_scheme_and_host(self):
        assert _normalize_url("HTTPS://Example.COM/") == "https://example.com"

    def test_strips_default_https_port(self):
        assert _normalize_url("https://example.com:443") == "https://example.com"

    def test_strips_default_http_port(self):
        assert _normalize_url("http://example.com:80") == "http://example.com"

    def test_keeps_non_default_port(self):
        assert _normalize_url("https://example.com:8443") == "https://example.com:8443"

    def test_preserves_path(self):
        assert _normalize_url("https://example.com/base/path/") == "https://example.com/base/path"

    def test_rejects_fragment(self):
        with pytest.raises(ValueError, match="fragment"):
            _normalize_url("https://example.com/#section")

    def test_rejects_query_string(self):
        with pytest.raises(ValueError, match="query string"):
            _normalize_url("https://example.com/?foo=bar")

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            _normalize_url("ftp://example.com")

    def test_rejects_missing_hostname(self):
        with pytest.raises(ValueError, match="missing hostname"):
            _normalize_url("https://")

    def test_strips_whitespace(self):
        assert _normalize_url("  https://example.com/  ") == "https://example.com"


# ---------------------------------------------------------------------------
# verify_xtm_jwt (async, mocked DB + JWKS fetch)
# ---------------------------------------------------------------------------


def _make_mock_db(
    *,
    trusted: bool = True,
    platform_url: str = "https://remote.test",
    api_url: str | None = None,
):
    """A session whose active registrations are one platform at *platform_url*
    (none when not *trusted*), reached on *api_url* when given."""
    mock_result = MagicMock()
    mock_result.all.return_value = (
        [SimpleNamespace(platform_identifier="openaev", platform_url=platform_url, api_url=api_url)]
        if trusted
        else []
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)
    return db


def _sign_remote_token(
    issuer: str, email: str, audience: str,
    private_key: Ed25519PrivateKey | None = None,
    kid: str | None = None, expired: bool = False,
) -> str:
    pk = private_key or _private_key
    k = kid or _kid
    now = datetime.now(UTC)
    payload = {
        "iss": issuer, "sub": str(uuid.uuid4()), "email": email, "aud": audience,
        "iat": now - timedelta(minutes=20) if expired else now,
        "exp": now - timedelta(minutes=10) if expired else now + timedelta(minutes=10),
        "jti": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, pk, algorithm="EdDSA", headers={"kid": k})


def _mock_signing_key(public_key_raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(public_key_raw)
    sk = MagicMock()
    sk.key = pub
    return sk


@pytest.mark.asyncio
class TestVerifyXtmJwt:
    async def test_valid_token(self):
        issuer = "https://remote.test"
        token = _sign_remote_token(issuer, "bob@example.com", _BASE_URL)
        db = _make_mock_db(trusted=True)

        with patch("app.core.xtm_auth._get_jwk_client") as mock_fn:
            client = MagicMock()
            client.get_signing_key_from_jwt.return_value = _mock_signing_key(_public_key_raw)
            mock_fn.return_value = client

            result = await verify_xtm_jwt(token, db)

        assert isinstance(result, CrossJWTPayload)
        assert result.iss == issuer
        assert result.email == "bob@example.com"
        assert result.aud == _BASE_URL
        assert result.jti
        assert result.platform_identifier == "openaev"

    async def test_untrusted_issuer_returns_none(self):
        token = _sign_remote_token("https://evil.test", "evil@example.com", _BASE_URL)
        db = _make_mock_db(trusted=False)
        assert await verify_xtm_jwt(token, db) is None

    async def test_missing_iss_claim_returns_none(self):
        now = datetime.now(UTC)
        payload = {"sub": str(uuid.uuid4()), "email": "a@b.com",
                   "iat": now, "exp": now + timedelta(minutes=10)}
        token = pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})
        db = _make_mock_db()
        assert await verify_xtm_jwt(token, db) is None

    async def test_missing_email_claim_returns_none(self):
        issuer = "https://remote.test"
        now = datetime.now(UTC)
        payload = {"iss": issuer, "sub": str(uuid.uuid4()), "aud": _BASE_URL,
                   "iat": now, "exp": now + timedelta(minutes=10), "jti": str(uuid.uuid4())}
        token = pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})
        db = _make_mock_db(trusted=True)

        with patch("app.core.xtm_auth._get_jwk_client") as mock_fn:
            client = MagicMock()
            client.get_signing_key_from_jwt.return_value = _mock_signing_key(_public_key_raw)
            mock_fn.return_value = client
            assert await verify_xtm_jwt(token, db) is None

    async def test_jwks_fetch_failure_returns_none(self):
        from jwt import PyJWKClientError
        token = _sign_remote_token("https://remote.test", "a@b.com", _BASE_URL)
        db = _make_mock_db(trusted=True)

        with patch("app.core.xtm_auth._get_jwk_client") as mock_fn:
            client = MagicMock()
            client.get_signing_key_from_jwt.side_effect = PyJWKClientError("connection refused")
            mock_fn.return_value = client
            assert await verify_xtm_jwt(token, db) is None

    async def test_expired_token_returns_none(self):
        token = _sign_remote_token("https://remote.test", "a@b.com", _BASE_URL, expired=True)
        db = _make_mock_db(trusted=True)

        with patch("app.core.xtm_auth._get_jwk_client") as mock_fn:
            client = MagicMock()
            client.get_signing_key_from_jwt.return_value = _mock_signing_key(_public_key_raw)
            mock_fn.return_value = client
            assert await verify_xtm_jwt(token, db) is None

    async def test_wrong_audience_returns_none(self):
        token = _sign_remote_token("https://remote.test", "a@b.com", "https://wrong.test")
        db = _make_mock_db(trusted=True)

        with patch("app.core.xtm_auth._get_jwk_client") as mock_fn:
            client = MagicMock()
            client.get_signing_key_from_jwt.return_value = _mock_signing_key(_public_key_raw)
            mock_fn.return_value = client
            assert await verify_xtm_jwt(token, db) is None


# ---------------------------------------------------------------------------
# JWKS → JWT round-trip (like a remote consumer)
# ---------------------------------------------------------------------------


class TestJwksRoundTrip:
    def test_consumer_can_verify_via_jwks(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        token = issue_xtm_jwt(uuid.uuid4(), "alice@example.com", "https://target.test")
        key_data = get_jwks_payload()["keys"][0]
        x_padded = key_data["x"] + "=" * (-len(key_data["x"]) % 4)
        pub_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(x_padded))
        decoded = pyjwt.decode(token, pub_key, algorithms=["EdDSA"], audience="https://target.test")
        assert decoded["email"] == "alice@example.com"

    def test_kid_in_jwt_matches_jwks(self):
        token = issue_xtm_jwt(uuid.uuid4(), "a@b.com", "https://target.test")
        header = pyjwt.get_unverified_header(token)
        jwks_kids = {k["kid"] for k in get_jwks_payload()["keys"]}
        assert header["kid"] in jwks_kids
