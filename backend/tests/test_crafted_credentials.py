"""A crafted credential is refused like a wrong one, never a 500.

Two families left alone:

* **bcrypt-checked credentials.** ``bcrypt==5.0.0`` raises ``ValueError`` for
  any input over 72 bytes, where bcrypt < 5 silently compared the first 72.
  Verification now compares those 72 bytes, as the hash was made: a stored
  hash of a longer secret (made by bcrypt < 5) keeps verifying it, and a
  crafted over-long value is an ordinary mismatch. A NEW password over 72
  bytes is refused by the password policy (400, like its other rules).
* **Credentials in a JSON body.** JSON can carry a lone surrogate
  (``"\\ud800"``), which Pydantic ``str`` fields accept and UTF-8 cannot
  encode, and any other type where a string belongs.

Follows the repo's fake-session convention: ``TestClient`` + dependency
overrides for the routes, fakes for the database and Redis; nothing real is
reached.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import bcrypt
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import deps
from app.api.v1 import auth as auth_api
from app.api.v1 import mcp_endpoint, mcp_oauth
from app.core import security
from app.core.database import get_db
from app.services import mfa, runners, token_store

SURROGATE = "\ud800"
#: A password longer than bcrypt reads, and what bcrypt < 5 stored for it.
LONG_PASSWORD = "Correct-horse-9" * 7
assert len(LONG_PASSWORD.encode()) > security.BCRYPT_MAX_BYTES


def _hash(secret: bytes) -> str:
    """A bcrypt hash as any version makes it for input within 72 bytes."""
    return bcrypt.hashpw(secret, bcrypt.gensalt(rounds=4)).decode()


#: bcrypt < 5 hashed the first 72 bytes of a longer input without a word.
LEGACY_HASH = _hash(LONG_PASSWORD.encode()[:72])
RIGHT = "Right-password-1"
RIGHT_HASH = _hash(RIGHT.encode())

#: Candidates no stored hash of ``RIGHT`` can match, in each crafted shape.
_CRAFTED = {
    "over-long": "x" * 200,
    "over-long-non-ascii": "\xe9" * 60,
    "right-then-junk-past-72-bytes": RIGHT + "y" * 100,
    "lone-surrogate": SURROGATE,
    "right-with-a-surrogate": RIGHT + SURROGATE,
}


# -- the bounded bcrypt input ---------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("a" * 73, b"a" * 72),
        ("\xe9" * 40, ("\xe9" * 36).encode()),
        (b"b" * 100, b"b" * 72),
        ("short", b"short"),
        (SURROGATE, SURROGATE.encode("utf-8", "surrogatepass")),
        (12345, None),
        (None, None),
        (["x"], None),
    ],
)
def test_bcrypt_input_is_what_bcrypt_compares_and_never_raises(candidate, expected):
    assert security.bcrypt_input(candidate) == expected


def test_a_hash_bcrypt_4_made_of_a_long_password_still_verifies_it():
    """The compatibility promise: nobody who logs in today is locked out."""
    assert security.verify_password(LONG_PASSWORD, LEGACY_HASH)
    assert not security.verify_password("Q" * len(LONG_PASSWORD), LEGACY_HASH)


@pytest.mark.parametrize("candidate", list(_CRAFTED.values()), ids=list(_CRAFTED))
def test_a_crafted_password_is_a_mismatch_not_a_raise(candidate):
    assert not security.verify_password(candidate, RIGHT_HASH)
    assert not security.verify_password("  " + candidate + "  ", RIGHT_HASH)


def test_the_right_password_still_verifies_with_or_without_whitespace():
    assert security.verify_password(RIGHT, RIGHT_HASH)
    assert security.verify_password(f"  {RIGHT} ", RIGHT_HASH)


@pytest.mark.parametrize(
    "candidate", [*list(_CRAFTED.values()), 12345, None, b"x" * 200], ids=[*_CRAFTED, "int", "none", "bytes"]
)
async def test_the_async_check_answers_every_shape_with_a_mismatch(candidate):
    assert await security.bcrypt_checkpw_async(candidate, RIGHT_HASH.encode()) is False


async def test_the_async_check_still_matches_the_right_key():
    assert await security.bcrypt_checkpw_async(RIGHT, RIGHT_HASH.encode())
    assert await security.bcrypt_checkpw_async(RIGHT.encode(), RIGHT_HASH.encode())


async def test_a_key_hashed_past_72_bytes_verifies_as_it_was_hashed():
    """An operator's long ADMIN_TOKEN: once a startup failure, now a working key."""
    token = ("fcp-" + "k" * 100).encode()
    hashed = await security.bcrypt_hashpw_async(token)
    assert await security.bcrypt_checkpw_async(token, hashed)
    assert not await security.bcrypt_checkpw_async(b"fcp-" + b"z" * 100, hashed)


# -- a new password is bounded by what bcrypt stores ----------------------------


@pytest.mark.parametrize(
    "password",
    ["aA1" * 25, "\xe9A1" * 20, "Valid-pass-1" + SURROGATE],
    ids=["75-ascii-bytes", "80-utf8-bytes", "lone-surrogate"],
)
def test_the_policy_refuses_a_password_bcrypt_cannot_store(password):
    with pytest.raises(ValueError):
        security.check_password_policy(password)
    with pytest.raises(HTTPException) as exc_info:
        deps.validate_password(password)
    assert exc_info.value.status_code == 400


def test_the_policy_still_accepts_a_password_of_exactly_72_bytes():
    password = "aA1" * 24
    assert len(password.encode()) == security.BCRYPT_MAX_BYTES
    security.check_password_policy(password)
    security.check_password_policy(f"  {password}  ")  # stripped before it is measured


def test_an_admin_password_bcrypt_cannot_store_is_a_configuration_error():
    """Once a bcrypt ValueError mid-startup; the rest of the policy stays off it."""
    from pydantic import ValidationError

    from app.core.config import Settings

    assert security.BCRYPT_MAX_BYTES == 72  # the literal the validator carries
    assert Settings(admin_password="a" * 72).admin_password == "a" * 72
    assert Settings(admin_password="abc").admin_password == "abc"
    with pytest.raises(ValidationError, match="admin_password"):
        Settings(admin_password="a" * 73)
    with pytest.raises(ValidationError, match="admin_password"):
        Settings(admin_password="\xe9" * 37)


# -- tokens from a JSON body ----------------------------------------------------


def test_a_token_hash_never_raises_and_matches_no_minted_token():
    assert security.hash_token(SURROGATE) != security.hash_token("")
    assert security.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


@pytest.mark.parametrize("token", [f"a{SURROGATE}.b.c", SURROGATE, "\xe9.\xe9.\xe9"])
def test_a_crafted_session_or_challenge_token_is_an_invalid_token(token):
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(token)
    with pytest.raises(mfa.MfaChallengeError):
        mfa.decode_challenge_token(token)


def test_a_pkce_verifier_that_is_not_ascii_matches_no_challenge():
    verifier = "v" * 43
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert mcp_oauth._hash_code_verifier(verifier) == digest.rstrip(b"=").decode()
    assert mcp_oauth._hash_code_verifier("\xe9" * 43) is None


@pytest.mark.parametrize("stored_challenge", ["", "some-challenge"])
def test_a_non_ascii_pkce_verifier_never_passes_the_exchange(oauth_client, monkeypatch, stored_challenge):
    """Not even against a code stored with an empty challenge."""

    async def _code(_code):
        return {
            "client_id": "c",
            "redirect_uri": "https://app.example/cb",
            "code_challenge": stored_challenge,
            "code_challenge_method": "S256",
            "user_id": str(uuid.uuid4()),
        }

    monkeypatch.setattr(mcp_oauth, "_consume_auth_code", _code)
    body = json.dumps(
        {"grant_type": "authorization_code", "code": "k", "code_verifier": "\xe9" * 43, "client_id": "c"}
    )
    resp = _post_json(oauth_client, "/mcp/oauth/token", body)
    assert (resp.status_code, resp.json()["error"]) == (400, "invalid_grant")


# -- the auth routes ------------------------------------------------------------


class _FakeDb:
    def __init__(self, user=None):
        self.user = user

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj, with_for_update=None):
        return None

    async def get(self, _model, _pk):
        return self.user

    async def execute(self, _stmt):
        user = self.user
        return SimpleNamespace(scalar_one_or_none=lambda: user)


class _NoTokens:
    """Redis holding no invitation and no reset token."""

    async def get(self, _key):
        return None

    async def getdel(self, _key):
        return None


def _user(hashed_password: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="u@example.com",
        full_name="Test User",
        role="user",
        is_active=True,
        is_admin=False,
        hashed_password=hashed_password,
        password_changed_at=None,
        sso_provider=None,
        mfa_enabled=False,
        mfa_secret=None,
        mfa_confirmed_at=None,
        mfa_last_used_step=None,
        mfa_recovery_codes=None,
    )


@pytest.fixture
def auth_app(monkeypatch):
    async def _nothing(*_a, **_kw):
        return None

    async def _no_tokens():
        return _NoTokens()

    async def _sso_config(_db):
        return SimpleNamespace(local_auth_enabled=True)

    import app.core.sso_helpers as sso_helpers

    monkeypatch.setattr(auth_api, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(auth_api, "log_audit", _nothing)
    monkeypatch.setattr(sso_helpers, "get_sso_config", _sso_config)
    monkeypatch.setattr(token_store, "redis_client", _no_tokens)
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")
    return app


def _client(app, user=None) -> TestClient:
    db = _FakeDb(user)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _post_json(client: TestClient, path: str, raw: str):
    """Post a JSON body written by hand, so a ``\\ud800`` escape reaches the server as is."""
    return client.post(path, content=raw.encode(), headers={"content-type": "application/json"})


@pytest.mark.parametrize("candidate", list(_CRAFTED.values()), ids=list(_CRAFTED))
def test_login_answers_a_crafted_password_with_401(auth_app, candidate):
    # ``json.dumps`` writes a lone surrogate as the ``\ud800`` escape a sender would.
    body = json.dumps({"email": "u@example.com", "password": candidate})
    resp = _post_json(_client(auth_app, _user(RIGHT_HASH)), "/api/v1/auth/login", body)
    assert resp.status_code == 401


def test_login_still_signs_in_a_user_whose_hash_bcrypt_4_made_of_a_long_password(auth_app):
    resp = _client(auth_app, _user(LEGACY_HASH)).post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": LONG_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_mfa_verify_answers_a_lone_surrogate_challenge_with_401(auth_app):
    body = '{"mfa_token": "a\\ud800b", "code": "123456"}'
    resp = _post_json(_client(auth_app), "/api/v1/auth/mfa/verify", body)
    assert resp.status_code == 401


def test_a_reset_to_a_password_bcrypt_cannot_store_is_a_400(auth_app):
    resp = _client(auth_app).post(
        "/api/v1/auth/reset-password", json={"token": "t", "new_password": "aA1" * 25}
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/auth/reset-password", '{"token": "\\ud800", "new_password": "Valid-pass-1"}'),
        (
            "/api/v1/auth/register-with-invitation",
            '{"token": "\\ud800", "password": "Valid-pass-1", "full_name": "N"}',
        ),
    ],
)
def test_a_lone_surrogate_link_token_is_an_invalid_link(auth_app, path, body):
    resp = _post_json(_client(auth_app), path, body)
    assert resp.status_code == 410


# -- the OAuth token and revocation endpoints -----------------------------------


class _OneClient:
    def __init__(self, client):
        self.client = client

    async def execute(self, _stmt):
        client = self.client
        return SimpleNamespace(scalar_one_or_none=lambda: client)


@pytest.fixture
def oauth_client(monkeypatch) -> TestClient:
    async def _nothing(*_a, **_kw):
        return None

    async def _no_redis():
        raise AssertionError("a refused request must not reach Redis")

    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(mcp_oauth, "redis_client", _no_redis)
    app = FastAPI()
    app.include_router(mcp_oauth.router, prefix="/mcp/oauth")

    async def _get_db():
        yield _OneClient(None)

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("[1, 2]", "invalid_request"),
        ("not json", "invalid_request"),
        ('{"grant_type": ["refresh_token"]}', "unsupported_grant_type"),
        ('{"grant_type": "refresh_token", "refresh_token": 123, "client_id": "c"}', "invalid_request"),
        ('{"grant_type": "refresh_token", "refresh_token": "\\ud800", "client_id": "c"}', "invalid_request"),
        (
            '{"grant_type": "authorization_code", "code": "\\ud800", "code_verifier": "v", "client_id": "c"}',
            "invalid_request",
        ),
    ],
)
def test_the_token_endpoint_answers_a_crafted_body_with_its_400(oauth_client, raw, error):
    resp = _post_json(oauth_client, "/mcp/oauth/token", raw)
    assert (resp.status_code, resp.json()["error"]) == (400, error)


@pytest.mark.parametrize("raw", ["[1]", '{"token": "\\ud800"}', '{"token": 42}'])
def test_the_revocation_endpoint_never_raises_on_a_crafted_body(oauth_client, raw):
    resp = _post_json(oauth_client, "/mcp/oauth/revoke", raw)
    assert resp.status_code in (200, 400)


@pytest.mark.parametrize("secret", ["x" * 200, "\xe9" * 60, SURROGATE, 12345, None])
async def test_a_crafted_client_secret_authenticates_no_client(secret):
    client = SimpleNamespace(client_secret_hash=RIGHT_HASH, grant_types=["client_credentials"])
    result = await mcp_oauth.authenticate_client(
        _OneClient(client), "client", secret, grant_type="client_credentials"
    )
    assert result is None


async def test_the_right_client_secret_still_authenticates():
    client = SimpleNamespace(client_secret_hash=RIGHT_HASH, grant_types=["client_credentials"])
    assert await mcp_oauth.authenticate_client(
        _OneClient(client), "client", RIGHT, grant_type="client_credentials"
    ) is client


# -- bcrypt-checked keys: /mcp/global, fcp- API keys, runner keys ---------------


def _bearer_request(path: str, credential: bytes) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"authorization", b"Bearer " + credential)],
            "query_string": b"",
        }
    )


@pytest.mark.parametrize(
    "credential",
    [b"mcp-" + b"A" * 100, b"mcp-" + "\xe9".encode() * 40, b"mcp-wrong"],
    ids=["over-long", "over-long-non-ascii", "wrong"],
)
async def test_the_global_mcp_endpoint_answers_a_crafted_key_with_401(monkeypatch, credential):
    import app.api.v1.platform_settings_helpers as ps_helpers

    async def _no_oauth(*_a, **_kw):
        return None

    async def _settings(_db):
        return SimpleNamespace(mcp_global_api_key_hash=_hash(b"mcp-right-key"))

    monkeypatch.setattr(mcp_endpoint, "try_oauth_token", _no_oauth)
    monkeypatch.setattr(ps_helpers, "get_platform_settings", _settings)
    with pytest.raises(HTTPException) as exc_info:
        await mcp_endpoint._authenticate_global_endpoint(
            request=_bearer_request("/mcp/global", credential), db=None
        )
    assert exc_info.value.status_code == 401


class _Candidates:
    def __init__(self, *rows):
        self.rows = list(rows)

    async def execute(self, _stmt):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


async def test_an_over_long_api_key_behind_a_known_prefix_is_a_401():
    from app.services.api_key_access import ApiKeySurface, resolve_api_key

    key = "fcp-abcdefgh-right-key"
    row = SimpleNamespace(key_hash=_hash(key.encode()), user_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await resolve_api_key(key + "A" * 100, _Candidates(row), surface=ApiKeySurface.REST)
    assert exc_info.value.status_code == 401


async def test_an_over_long_runner_key_behind_a_known_prefix_resolves_nothing():
    key = "run-abcdefgh-right-key"
    runner = SimpleNamespace(key_hash=_hash(key.encode()))
    assert await runners.resolve_runner_credential(key + "\xe9" * 60, _Candidates(runner)) is None
    assert await runners.resolve_runner_credential(key, _Candidates(runner)) is runner
