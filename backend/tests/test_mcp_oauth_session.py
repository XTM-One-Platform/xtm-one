"""Tests for the session-based MCP OAuth consent path.

The standalone consent page only offered a local email/password form, so
SSO-only users (no local ``hashed_password``) could never complete the MCP
OAuth flow. The fix adds:

* ``POST /mcp/oauth/authorize/session`` — approve an MCP client from an
  existing platform session (JWT) instead of a local password; and
* an SSO-aware consent page that detects a session, offers SSO providers, or
  falls back to the local form.

These tests pin the session endpoint's auth + validation contract and the
consent page's three-path wiring. They drive the route functions directly
with lightweight stubs (the repo has no FastAPI test client / DB fixture —
see ``test_mcp_oauth_authorize.py``).
"""

from __future__ import annotations

import json as _json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.api.v1 import mcp_oauth as mcp_oauth_module
from app.api.v1.mcp_oauth import authorize_session
from app.core import security

REDIRECT_URI = "https://localhost:8723/callback"


class _StubDB:
    """Returns queued ``scalar_one_or_none`` results in call order.

    For the session endpoint the call order is: (1) the user lookup in
    ``_resolve_session_user``, then (2) the OAuthClient lookup.
    """

    def __init__(self, results: list[object]):
        self._results = list(results)
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        value = self._results.pop(0) if self._results else None

        class _R:
            def scalar_one_or_none(self):
                return value

        return _R()

    def add(self, _obj):  # pragma: no cover - log_audit is stubbed
        pass

    async def flush(self):  # pragma: no cover - log_audit is stubbed
        pass

    async def commit(self):
        pass


class _Req:
    def __init__(
        self,
        *,
        bearer: str | None = "tok",
        body: dict | None = None,
        scheme: str = "Bearer",
    ):
        headers: dict[str, str] = {}
        if bearer is not None:
            headers["Authorization"] = f"{scheme} {bearer}"
        self.headers = headers
        self._body = body if body is not None else {}
        self.client = SimpleNamespace(host="127.0.0.1")

    async def json(self):
        return self._body


def _make_user(*, active: bool = True, password_changed_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="sso.user@example.com",
        full_name="SSO User",
        is_active=active,
        password_changed_at=password_changed_at,
    )


def _make_client(redirect_uris: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        client_id="mcp-client1",
        client_name="VS Code",
        redirect_uris=redirect_uris if redirect_uris is not None else [REDIRECT_URI],
    )


def _body(**overrides) -> dict:
    body = {
        "client_id": "mcp-client1",
        "redirect_uri": REDIRECT_URI,
        "state": "st-123",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "response_type": "code",
        "resource": "",
    }
    body.update(overrides)
    return body


def _patch_grant(monkeypatch) -> None:
    """Stub the Redis code store + audit log used by the grant helper."""

    async def _store(*_a, **_kw):
        return None

    async def _audit(*_a, **_kw):
        return None

    monkeypatch.setattr(mcp_oauth_module, "_store_auth_code", _store)
    monkeypatch.setattr(mcp_oauth_module, "log_audit", _audit)


def _patch_decode(monkeypatch, payload):
    monkeypatch.setattr(security, "decode_access_token", lambda _t: payload)


# ── Happy path ──────────────────────────────────────────────────────────


async def test_session_success_returns_redirect_url(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)

    db = _StubDB([user, _make_client()])
    resp = await authorize_session(request=_Req(body=_body()), db=db)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    data = _json.loads(resp.body)
    assert data["redirect_url"].startswith(REDIRECT_URI)
    assert "code=" in data["redirect_url"]
    assert "state=st-123" in data["redirect_url"]


async def test_session_omits_empty_state(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)

    db = _StubDB([user, _make_client()])
    resp = await authorize_session(request=_Req(body=_body(state="")), db=db)

    data = _json.loads(resp.body)
    assert "state=" not in data["redirect_url"]


async def test_session_accepts_case_insensitive_bearer_scheme(monkeypatch):
    """HTTP auth schemes are case-insensitive (RFC 7235) and FastAPI's
    ``HTTPBearer`` accepts e.g. ``bearer`` — a lowercase scheme must resolve
    the session, not 401."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)

    db = _StubDB([user, _make_client()])
    resp = await authorize_session(
        request=_Req(body=_body(), scheme="bearer"), db=db
    )

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    assert "code=" in _json.loads(resp.body)["redirect_url"]


# ── Auth failures ───────────────────────────────────────────────────────


async def test_session_missing_bearer(monkeypatch):
    db = _StubDB([])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(bearer=None, body=_body()), db=db)
    assert exc.value.status_code == 401
    assert db.execute_calls == 0


async def test_session_invalid_token(monkeypatch):
    def _boom(_t):
        raise ValueError("bad token")

    monkeypatch.setattr(security, "decode_access_token", _boom)
    db = _StubDB([])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 401
    assert "Invalid or expired" in exc.value.detail


async def test_session_rejects_mcp_oauth_token(monkeypatch):
    """Token-type confusion: an MCP *access* token (type=mcp_oauth) must not be
    replayable here to authorize further grants."""
    payload = {"sub": str(uuid.uuid4()), "type": "mcp_oauth", "iat": 9_999_999_999}
    _patch_decode(monkeypatch, payload)
    db = _StubDB([])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 401
    assert "Invalid token type" in exc.value.detail


async def test_session_rejects_the_mfa_challenge_token(monkeypatch):
    """The MFA challenge (``scope: mfa``, first factor only) is not a session:
    replayed as the bearer here it must not grant a code without the second
    factor. Decoded for real, not stubbed, and refused before any
    user lookup."""
    from app.services import mfa

    stored: list[object] = []

    async def _store(*_a, **_kw):
        stored.append(_kw)

    monkeypatch.setattr(mcp_oauth_module, "_store_auth_code", _store)
    challenge = mfa.create_challenge_token(uuid.uuid4())
    db = _StubDB([_make_user(), _make_client()])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(bearer=challenge, body=_body()), db=db)
    assert exc.value.status_code == 401
    assert "Invalid token type" in exc.value.detail
    assert db.execute_calls == 0
    assert stored == []


async def test_session_user_not_found(monkeypatch):
    _patch_decode(monkeypatch, {"sub": str(uuid.uuid4()), "iat": 9_999_999_999})
    db = _StubDB([None])  # user lookup → None
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 401


async def test_session_inactive_user(monkeypatch):
    user = _make_user(active=False)
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    db = _StubDB([user])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 401


async def test_session_stale_after_password_change(monkeypatch):
    changed = datetime(2025, 6, 1, tzinfo=UTC)
    user = _make_user(password_changed_at=changed)
    iat = int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())  # before change
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": iat})
    db = _StubDB([user])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


# ── OAuth-param + client validation ─────────────────────────────────────


async def test_session_missing_code_challenge(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    db = _StubDB([user])
    body = _body()
    del body["code_challenge"]
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=body), db=db)
    assert exc.value.status_code == 400


async def test_session_rejects_non_code_response_type(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    db = _StubDB([user])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body(response_type="token")), db=db)
    assert exc.value.status_code == 400


async def test_session_unknown_client(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    db = _StubDB([user, None])  # user ok, client lookup → None
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 400
    assert "client_id" in exc.value.detail


async def test_session_redirect_uri_mismatch(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    client = _make_client(redirect_uris=["https://registered.example.com/cb"])
    db = _StubDB([user, client])
    body = _body(redirect_uri="https://evil.example.com/cb")
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=body), db=db)
    assert exc.value.status_code == 400
    assert "redirect_uri" in exc.value.detail


async def test_session_rejects_when_no_redirect_uris_registered(monkeypatch):
    """A client registered with an empty ``redirect_uris`` list must NOT be
    treated as "allow any redirect_uri" — an empty list means no URIs are
    registered, so any provided redirect_uri is rejected (otherwise a client
    registered without redirect_uris could have a code sent anywhere)."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    client = _make_client(redirect_uris=[])  # nothing registered
    db = _StubDB([user, client])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body()), db=db)
    assert exc.value.status_code == 400
    assert "redirect_uri" in exc.value.detail


async def test_session_rejects_non_string_field(monkeypatch):
    """A malformed payload (non-string OAuth param) must be a clean 400, not an
    AttributeError 500 from calling ``.strip()`` on a dict/list."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    db = _StubDB([user])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body(client_id={})), db=db)
    assert exc.value.status_code == 400
    assert "client_id" in exc.value.detail


async def test_session_rejects_unsupported_code_challenge_method(monkeypatch):
    """An unsupported PKCE method must be rejected before a code is minted —
    the token endpoint only verifies S256, so the grant would otherwise be
    un-exchangeable."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    db = _StubDB([user, _make_client()])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(
            request=_Req(body=_body(code_challenge_method="bogus")), db=db
        )
    assert exc.value.status_code == 400
    assert "code_challenge_method" in exc.value.detail


async def test_session_rejects_plain_code_challenge_method(monkeypatch):
    """``plain`` is S256-only-rejected now: OAuth 2.1 drops it and the
    authorization-server metadata advertises only S256, so accepting it here
    would make discovery metadata inaccurate (and the method is weaker)."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    db = _StubDB([user, _make_client()])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(
            request=_Req(body=_body(code_challenge_method="plain")), db=db
        )
    assert exc.value.status_code == 400
    assert "code_challenge_method" in exc.value.detail


async def test_session_rejects_redirect_uri_with_fragment(monkeypatch):
    """A registered ``redirect_uri`` carrying a ``#fragment`` must be refused:
    the redirect is built by appending ``?code=...``, so a fragment would put
    the code in the fragment and break the exchange (and violate OAuth)."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    frag_uri = f"{REDIRECT_URI}#section"
    client = _make_client(redirect_uris=[frag_uri])
    db = _StubDB([user, client])
    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body(redirect_uri=frag_uri)), db=db)
    assert exc.value.status_code == 400
    assert "fragment" in exc.value.detail.lower()


# ── Consent page wiring ─────────────────────────────────────────────────


def test_consent_html_has_three_paths():
    html = mcp_oauth_module._consent_html(
        client_name="VS Code",
        client_id="mcp-client1",
        redirect_uri=REDIRECT_URI,
        app_name="XTM One",
    )
    # Session path
    assert "authorizeWithSession" in html
    assert "/mcp/oauth/authorize/session" in html
    assert "localStorage.getItem('copilot_token')" in html
    # SSO discovery path
    assert "/api/v1/auth/config" in html
    assert "/api/auth/oidc/login" in html
    assert "/api/auth/saml/login" in html
    assert "mcp_oauth_return" in html
    # Local fallback path (form still present)
    assert 'id="local-auth-form"' in html


def test_consent_html_runs_sso_discovery_even_with_session():
    """SSO discovery must be wired to run regardless of a stored session.

    Regression guard for the early-return that skipped SSO discovery when a
    ``copilot_token`` was present: if that token was expired, the 401 fallback
    revealed a login section with no SSO buttons, stranding SSO-only users.
    The script must define ``setupLoginSection`` and await it (via
    ``_loginSectionReady``) so the revealed login section is always configured.
    """
    html = mcp_oauth_module._consent_html(
        client_name="VS Code",
        client_id="mcp-client1",
        redirect_uri=REDIRECT_URI,
        app_name="XTM One",
    )
    assert "function setupLoginSection" in html
    assert "_loginSectionReady" in html
    # The fallback path must await discovery before revealing the login section.
    assert "await _loginSectionReady" in html


# ── redirect_uri transport validation ───────────────────────────────────


def test_validate_redirect_uri_enforces_scheme():
    """Loopback callbacks may use http or https; every other host must use
    https. An unexpected scheme (e.g. ftp) must be rejected even for loopback,
    so it can't be registered or receive an authorization code."""
    ok = (
        "http://localhost:8723/callback",
        "http://127.0.0.1/cb",
        "https://localhost/cb",
        "https://[::1]/cb",
        "https://app.example.com/cb",
    )
    bad = (
        "ftp://localhost/cb",  # non-http(s) loopback scheme
        "file://localhost/cb",
        "http://app.example.com/cb",  # non-loopback must be https
        "ftp://app.example.com/cb",
    )
    for uri in ok:
        assert mcp_oauth_module._validate_redirect_uri(uri) is True, uri
    for uri in bad:
        assert mcp_oauth_module._validate_redirect_uri(uri) is False, uri


# ── Product sign-in, authorized with no consent page ────────────


def _platform_client() -> SimpleNamespace:
    return SimpleNamespace(
        client_id="xtm-platform-opencrq",
        client_name="OpenCRQ",
        redirect_uris=[REDIRECT_URI],
        scope="openid email profile xtm_role",
    )


def _platform_active(monkeypatch, active: bool) -> None:
    async def _is_active(_db, _client_id):
        return active

    monkeypatch.setattr(mcp_oauth_module, "is_active_platform_client", _is_active)


_SIGN_IN = {"client_id": "xtm-platform-opencrq", "scope": "openid email", "nonce": "n"}


async def test_a_product_sign_in_goes_through_on_a_first_hand_session(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    _platform_active(monkeypatch, True)

    resp = await authorize_session(request=_Req(body=_body(**_SIGN_IN)), db=_StubDB([user, _platform_client()]))

    assert "code=" in _json.loads(resp.body)["redirect_url"]


async def test_a_product_sign_in_refuses_an_impersonation(monkeypatch):
    """No consent page, so no landing in a product as the impersonated user."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999, "impersonated_by": str(uuid.uuid4())})
    _patch_grant(monkeypatch)
    _platform_active(monkeypatch, True)

    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body(**_SIGN_IN)), db=_StubDB([user, _platform_client()]))
    assert exc.value.status_code == 403


async def test_an_mcp_client_consent_while_impersonating_is_unchanged(monkeypatch):
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999, "impersonated_by": str(uuid.uuid4())})
    _patch_grant(monkeypatch)

    resp = await authorize_session(request=_Req(body=_body(scope="openid")), db=_StubDB([user, _make_client()]))

    assert "code=" in _json.loads(resp.body)["redirect_url"]


async def test_a_product_sign_in_that_can_no_longer_be_granted_is_refused(monkeypatch):
    """The page decided a product sign-in; its registration went away before the
    POST. Refused, never a silent MCP authorization instead."""
    user = _make_user()
    _patch_decode(monkeypatch, {"sub": str(user.id), "iat": 9_999_999_999})
    _patch_grant(monkeypatch)
    _platform_active(monkeypatch, False)

    with pytest.raises(HTTPException) as exc:
        await authorize_session(request=_Req(body=_body(**_SIGN_IN)), db=_StubDB([user, _platform_client()]))
    assert exc.value.status_code == 400
