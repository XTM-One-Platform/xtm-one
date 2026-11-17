"""OpenID Connect on the platform's OAuth server, for product sign-in.

A client registered for ``openid`` and asking for it gets an ID token beside
the access token, signed with the platform key published at ``/xtm/auth/jwks``,
and finds both through ``/.well-known/openid-configuration``. Claims follow the
granted scope (``email``, ``profile``, ``xtm_role``). A client that never
registered for ``openid`` gets nothing new. Route functions are driven directly
with stubs, as in ``test_mcp_oauth_session.py``; discovery also over HTTP.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.v1 import mcp_auth, mcp_oauth
from app.core import security, xtm_auth
from app.core.base_path import BasePathMiddleware
from app.core.config import settings
from app.core.xtm_auth import get_jwks_payload

CLIENT_ID = "xtm-platform-opencrq"
REDIRECT_URI = "https://opencrq.example.com/auth/callback"
VERIFIER = "v" * 64
ALL_SCOPES = "openid email profile xtm_role"


def _user(*, active: bool = True, title: str | None = "CISO") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="jane.doe@example.com",
        full_name="Jane Doe",
        title=title,
        role="admin",
        is_active=active,
        hashed_password=None,
        password_changed_at=None,
    )


def _client(scope: str | None = ALL_SCOPES, client_id: str = CLIENT_ID) -> SimpleNamespace:
    """A platform's sign-in client by default."""
    return SimpleNamespace(client_id=client_id, client_name="OpenCRQ", redirect_uris=[REDIRECT_URI], scope=scope)


async def _no_op(*_a, **_kw):
    return None


@pytest.fixture(autouse=True)
def _active_platform(monkeypatch):
    """The client's platform registration is active unless a test says otherwise."""
    state = {"active": True}

    async def _is_active(_db, _client_id):
        return state["active"]

    monkeypatch.setattr(mcp_oauth, "is_active_platform_client", _is_active)
    return state


# ── Discovery (over HTTP) ────────────────────────────────────────────────────


def _discovery_client(monkeypatch, base_path: str) -> TestClient:
    monkeypatch.setattr(settings, "base_path", base_path)
    monkeypatch.setattr(settings, "base_url", f"https://intranet.example.com{base_path}")
    app = FastAPI()
    app.include_router(mcp_oauth.well_known_router, prefix="/.well-known")
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


def test_discovery_is_served_whole_over_http(monkeypatch):
    client = _discovery_client(monkeypatch, "")
    oauth = client.get("/.well-known/oauth-authorization-server")
    resp = client.get("/.well-known/openid-configuration")

    assert resp.status_code == 200
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert resp.headers.get_list("content-type") == ["application/json"]
    doc = resp.json()
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert doc[key] == oauth.json()[key]
    assert doc["jwks_uri"] == "https://intranet.example.com/xtm/auth/jwks"
    assert doc["scopes_supported"] == ["openid", "email", "profile", "xtm_role"]
    assert doc["id_token_signing_alg_values_supported"] == ["EdDSA"]
    assert {"name", "xtm_title", "xtm_role", "xtm_super_admin"} <= set(doc["claims_supported"])


def test_discovery_under_a_base_path_answers_in_both_shapes(monkeypatch):
    """OIDC Discovery appends the well-known segment to the issuer
    (``/xtm-one/.well-known/...``), RFC 8414 inserts it at the host root."""
    client = _discovery_client(monkeypatch, "/xtm-one")
    appended = client.get("/xtm-one/.well-known/openid-configuration")
    inserted = client.get("/.well-known/openid-configuration/xtm-one")

    assert appended.status_code == inserted.status_code == 200
    assert appended.json() == inserted.json()
    assert appended.json()["issuer"] == "https://intranet.example.com/xtm-one"
    assert int(appended.headers["content-length"]) == len(appended.content)
    assert client.get("/.well-known/openid-configuration/other").status_code == 404


# ── Granted scope ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("asked", "registered", "granted"),
    [
        ("openid email profile xtm_role", ALL_SCOPES, ALL_SCOPES),
        ("openid profile email", "openid email", "openid email"),
        ("openid unknown email", ALL_SCOPES, "openid email"),
        # A client that never registered for sign-in (an MCP client sending
        # scope=openid today) gets no OpenID Connect at all.
        ("openid email", None, ""),
        ("openid email", "mcp", ""),
        ("email profile", ALL_SCOPES, ""),
    ],
)
def test_the_granted_scope_is_what_the_client_asked_and_registered(asked, registered, granted):
    assert mcp_oauth._granted_oidc_scope(asked, _client(registered)) == granted


def test_only_a_platform_sign_in_client_is_granted_openid_connect():
    """A dynamically registered row that kept OpenID Connect scopes (registered
    before they were stripped at registration) is granted none."""
    assert mcp_oauth._granted_oidc_scope(ALL_SCOPES, _client(ALL_SCOPES, client_id="mcp-0123456789abcdef")) == ""


# ── Token endpoint ───────────────────────────────────────────────────────────


class _UserDB:
    def __init__(self, user):
        self._user = user

    async def get(self, _model, _id):
        return self._user


async def _exchange(
    monkeypatch, user, *, scope: str | None, nonce: str | None = "n-123", client_id: str = CLIENT_ID, resource: str | None = None
):
    stored_refresh: list[dict] = []

    async def _consume(_code):
        return {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "user_id": str(user.id),
            "code_challenge": mcp_oauth._hash_code_verifier(VERIFIER),
            "code_challenge_method": "S256",
            "resource": resource,
            "scope": scope,
            "nonce": nonce,
        }

    async def _store(token_hash, **kwargs):
        stored_refresh.append(kwargs)

    monkeypatch.setattr(mcp_oauth, "_consume_auth_code", _consume)
    monkeypatch.setattr(mcp_oauth, "_store_refresh_token", _store)
    monkeypatch.setattr(mcp_oauth, "_authenticate_token_client", _no_op)
    response = await mcp_oauth._handle_authorization_code(
        {"code": "c", "code_verifier": VERIFIER, "client_id": client_id, "redirect_uri": REDIRECT_URI},
        _UserDB(user),
    )
    return response, stored_refresh


def _verify(id_token: str) -> dict:
    key = jwt.PyJWK(get_jwks_payload()["keys"][0])
    return jwt.decode(id_token, key.key, algorithms=["EdDSA"], audience=CLIENT_ID, issuer=mcp_oauth._base_url())


async def test_the_id_token_carries_the_claims_of_the_granted_scopes(monkeypatch):
    user = _user()
    response, _ = await _exchange(monkeypatch, user, scope=ALL_SCOPES)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["access_token"]
    assert body["scope"] == ALL_SCOPES
    claims = _verify(body["id_token"])
    assert claims["sub"] == str(user.id)
    assert claims["nonce"] == "n-123"
    assert claims["email"] == "jane.doe@example.com"
    assert claims["name"] == "Jane Doe"
    assert claims["xtm_title"] == "CISO"
    assert claims["xtm_role"] == "admin"
    assert claims["xtm_super_admin"] is False


@pytest.mark.parametrize(
    ("admin_email", "super_admin"),
    [("jane.doe@example.com", True), ("admin@filigran.io", False)],
    ids=["admin-email-account", "another-administrator"],
)
async def test_the_xtm_role_scope_says_whether_the_person_is_the_admin_email_account(
    monkeypatch, admin_email, super_admin
):
    """True only for the ``ADMIN_EMAIL`` account: the ``admin`` role alone is not enough."""
    monkeypatch.setattr(settings, "admin_email", admin_email)
    response, _ = await _exchange(monkeypatch, _user(), scope="openid xtm_role")

    claims = _verify(json.loads(response.body)["id_token"])
    assert claims["xtm_role"] == "admin"
    assert claims["xtm_super_admin"] is super_admin


async def test_super_admin_is_only_said_under_the_xtm_role_scope(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", "jane.doe@example.com")
    response, _ = await _exchange(monkeypatch, _user(), scope="openid email profile")

    assert "xtm_super_admin" not in _verify(json.loads(response.body)["id_token"])


@pytest.mark.parametrize("title", [None, ""], ids=["none", "empty"])
async def test_the_profile_scope_carries_a_null_title_for_a_person_without_one(monkeypatch, title):
    """Present and null, so a client tells "no title" from a server that does not emit the claim."""
    response, _ = await _exchange(monkeypatch, _user(title=title), scope="openid profile")

    claims = _verify(json.loads(response.body)["id_token"])
    assert "xtm_title" in claims
    assert claims["xtm_title"] is None
    assert claims["name"] == "Jane Doe"


async def test_the_title_is_only_shared_under_the_profile_scope(monkeypatch):
    response, _ = await _exchange(monkeypatch, _user(), scope="openid email xtm_role")

    assert "xtm_title" not in _verify(json.loads(response.body)["id_token"])


async def test_a_product_sign_in_client_gets_no_refresh_token(monkeypatch):
    """Authorized without consent: a leaked code never becomes a lasting access."""
    response, stored = await _exchange(monkeypatch, _user(), scope=ALL_SCOPES)

    body = json.loads(response.body)
    assert body["id_token"] and body["access_token"]
    assert "refresh_token" not in body
    assert stored == []


async def test_an_mcp_client_still_gets_its_refresh_token(monkeypatch):
    response, stored = await _exchange(monkeypatch, _user(), scope=None, nonce=None, client_id="mcp-client-1")

    assert json.loads(response.body)["refresh_token"]
    assert len(stored) == 1


class _OneUserSession:
    """The lookup ``mcp_auth.try_oauth_token`` makes once the token itself is accepted."""

    def __init__(self, user):
        self._user = user

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._user)


@pytest.mark.parametrize("sent_resource", [None, "/mcp/user", "/mcp/global"])
async def test_a_product_sign_in_access_token_opens_no_mcp_endpoint(monkeypatch, sent_resource):
    """Authorized without consent, so its access token must be useless on
    every MCP resource server, whatever ``resource`` the client sent."""
    user = _user()
    resource = f"{settings.base_url.rstrip('/')}{sent_resource}" if sent_resource else None
    response, _ = await _exchange(monkeypatch, user, scope=ALL_SCOPES, resource=resource)

    token = json.loads(response.body)["access_token"]
    assert jwt.decode(token, options={"verify_signature": False})["aud"] == CLIENT_ID
    for path in ("mcp/user", "mcp/global"):
        assert await mcp_auth.try_oauth_token(token, _OneUserSession(user), path) is None


async def test_an_mcp_client_token_still_opens_its_resource(monkeypatch):
    """The counter-case: the same exchange for an MCP client keeps its audience."""
    user = _user()
    resource = f"{settings.base_url.rstrip('/')}/mcp/user"
    response, _ = await _exchange(monkeypatch, user, scope=None, nonce=None, client_id="mcp-client-1", resource=resource)

    token = json.loads(response.body)["access_token"]
    assert await mcp_auth.try_oauth_token(token, _OneUserSession(user), "mcp/user") is user


async def test_a_platform_client_without_openid_keeps_its_refresh_token(monkeypatch):
    """No ``openid``, so no product sign-in: it went through the consent page."""
    response, stored = await _exchange(monkeypatch, _user(), scope=None, nonce=None)

    assert json.loads(response.body)["refresh_token"]
    assert len(stored) == 1


@pytest.mark.parametrize(
    ("login_method", "scope", "consent"),
    [
        ("mcp_oauth_session", "openid email", False),
        ("mcp_oauth_session", "", None),
        ("mcp_oauth", "openid email", None),
    ],
)
async def test_the_audit_tells_a_sign_in_without_consent_apart(monkeypatch, login_method, scope, consent):
    audited: list[dict] = []

    async def _granted(_db, requested, _client):
        return requested

    async def _audit(_db, **kwargs):
        audited.append(kwargs["details"])

    monkeypatch.setattr(mcp_oauth, "_granted_oidc_scope_for", _granted)
    monkeypatch.setattr(mcp_oauth, "_store_auth_code", _no_op)
    monkeypatch.setattr(mcp_oauth, "log_audit", _audit)
    await mcp_oauth._grant_authorization_code(
        None,
        user=_user(),
        client_row=_client(),
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="s",
        code_challenge="c" * 43,
        code_challenge_method="S256",
        resource="",
        request=SimpleNamespace(),
        login_method=login_method,
        scope=scope,
        nonce="n",
    )

    assert audited[0]["login_method"] == login_method
    assert audited[0].get("consent") is consent


async def test_an_mcp_client_asking_openid_is_not_audited_as_a_sign_in_without_consent(monkeypatch):
    """The real grant (no stub): an MCP client sending ``scope=openid`` gets no
    OpenID Connect scope, clicked through the consent page, and is audited as such."""
    audited: list[dict] = []

    async def _audit(_db, **kwargs):
        audited.append(kwargs["details"])

    monkeypatch.setattr(mcp_oauth, "_store_auth_code", _no_op)
    monkeypatch.setattr(mcp_oauth, "log_audit", _audit)
    await mcp_oauth._grant_authorization_code(
        None,
        user=_user(),
        client_row=_client(scope=None, client_id="mcp-client-1"),
        client_id="mcp-client-1",
        redirect_uri=REDIRECT_URI,
        state="s",
        code_challenge="c" * 43,
        code_challenge_method="S256",
        resource="",
        request=SimpleNamespace(),
        login_method="mcp_oauth_session",
        scope="openid",
        nonce="n",
    )

    assert "consent" not in audited[0]


async def _grant_audited(monkeypatch, *, granted: str, scope: str = "openid email", client_id: str = CLIENT_ID) -> list[dict]:
    audited: list[dict] = []

    async def _granted(_db, _requested, _client):
        return granted

    async def _audit(_db, **kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(mcp_oauth, "_granted_oidc_scope_for", _granted)
    monkeypatch.setattr(mcp_oauth, "_store_auth_code", _no_op)
    monkeypatch.setattr(mcp_oauth, "log_audit", _audit)
    await mcp_oauth._grant_authorization_code(
        None,
        user=_user(),
        client_row=_client(client_id=client_id),
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        state="s",
        code_challenge="c" * 43,
        code_challenge_method="S256",
        resource="",
        request=SimpleNamespace(),
        login_method="mcp_oauth_session",
        scope=scope,
        nonce="n",
    )
    return audited


async def test_a_product_sign_in_no_longer_grantable_at_the_grant_is_refused(monkeypatch):
    """Decided once, where the code is minted: a registration that went inactive
    after the page (or after any earlier check) never yields an MCP code."""
    with pytest.raises(HTTPException) as exc:
        await _grant_audited(monkeypatch, granted="")
    assert exc.value.status_code == 400


async def test_a_product_sign_in_is_audited_as_a_sign_in(monkeypatch):
    audited = await _grant_audited(monkeypatch, granted="openid email")

    assert audited[0]["summary"].startswith("Signed in to OpenCRQ through XTM One")
    assert audited[0]["details"]["consent"] is False


async def test_an_mcp_grant_keeps_its_audit_summary(monkeypatch):
    audited = await _grant_audited(monkeypatch, granted="", scope="", client_id="mcp-client-1")

    assert audited[0]["summary"].startswith("Authorized MCP OAuth client")
    assert "consent" not in audited[0]["details"]


async def test_openid_alone_shares_no_personal_claim(monkeypatch):
    response, _ = await _exchange(monkeypatch, _user(), scope="openid")

    claims = _verify(json.loads(response.body)["id_token"])
    assert set(claims) == {"iss", "sub", "aud", "iat", "exp", "nonce"}


async def test_without_openid_the_response_is_unchanged(monkeypatch):
    response, _ = await _exchange(monkeypatch, _user(), scope=None, nonce=None)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert "id_token" not in body
    assert "scope" not in body


async def test_a_platform_no_longer_registered_gets_no_token_at_all(monkeypatch, _active_platform):
    """Unregistered or purged between the code and its exchange."""
    _active_platform["active"] = False
    response, stored_refresh = await _exchange(monkeypatch, _user(), scope="openid")

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_grant"
    assert stored_refresh == []


async def test_a_platform_no_longer_registered_signs_nobody_in(monkeypatch, _active_platform):
    _active_platform["active"] = False
    stored = _capture_codes(monkeypatch)
    # Refused, never minted as a code without OpenID Connect.
    with pytest.raises(HTTPException) as exc:
        await _grant(scope="openid email", nonce="n")

    assert exc.value.status_code == 400
    assert stored == []
    page = await _consent_page(monkeypatch, _client(), "openid email")
    assert '<input type="hidden" name="product_sign_in" value="">' in page


async def test_a_disabled_account_gets_no_token_at_all(monkeypatch):
    response, stored_refresh = await _exchange(monkeypatch, _user(active=False), scope="openid")

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_grant"
    assert stored_refresh == []


def test_a_cross_platform_verifier_refuses_an_id_token():
    """Same key as the cross-platform JWTs: the audience (the client, never a
    platform URL) and the missing ``jti`` keep an ID token from passing for one."""
    token = mcp_oauth._create_id_token(_user(), client_id=CLIENT_ID, nonce=None, scope=ALL_SCOPES)
    claims = jwt.decode(token, options={"verify_signature": False})

    with pytest.raises(jwt.InvalidAudienceError):
        xtm_auth._check_audience(claims["aud"])
    assert "jti" not in claims


# ── Authorization endpoint ───────────────────────────────────────────────────


def _capture_codes(monkeypatch) -> list[dict]:
    stored: list[dict] = []

    async def _store(code, **kwargs):
        stored.append(kwargs)

    monkeypatch.setattr(mcp_oauth, "_store_auth_code", _store)
    monkeypatch.setattr(mcp_oauth, "log_audit", _no_op)
    return stored


async def _grant(**overrides):
    kwargs = {
        "user": _user(),
        "client_row": _client(),
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": "st",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "resource": "",
        "request": None,
        "login_method": "mcp_oauth_session",
    }
    kwargs.update(overrides)
    return await mcp_oauth._grant_authorization_code(None, **kwargs)


async def test_the_code_carries_the_granted_scope_and_the_nonce(monkeypatch):
    stored = _capture_codes(monkeypatch)
    target = await _grant(scope="openid email unknown", nonce="n-456")

    assert target.startswith(REDIRECT_URI)
    assert stored[0]["scope"] == "openid email"
    assert stored[0]["nonce"] == "n-456"


async def test_no_nonce_is_kept_for_a_client_not_registered_for_openid(monkeypatch):
    stored = _capture_codes(monkeypatch)
    await _grant(client_row=_client(None, client_id="mcp-client-1"), client_id="mcp-client-1", scope="openid", nonce="n")

    assert stored[0]["scope"] is None
    assert stored[0]["nonce"] is None


async def test_an_oversized_nonce_is_refused_before_the_sign_in_page(monkeypatch):
    async def _load(*_a, **_kw):
        return _client()

    monkeypatch.setattr(mcp_oauth, "_load_client", _load)
    with pytest.raises(HTTPException) as exc:
        await mcp_oauth.authorize_get(
            request=SimpleNamespace(),
            response_type="code",
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_challenge="challenge",
            code_challenge_method="S256",
            state="",
            resource="",
            scope="openid",
            nonce="n" * (mcp_oauth.MAX_NONCE_LENGTH + 1),
            db=None,
        )
    assert exc.value.status_code == 400


async def test_the_session_path_forwards_scope_and_nonce(monkeypatch):
    stored = _capture_codes(monkeypatch)

    async def _session_user(_request, _db):
        return _user()

    async def _load(*_a, **_kw):
        return _client()

    class _Req:
        headers = {"Authorization": "Bearer t"}
        client = SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": "challenge",
                "scope": "openid profile",
                "nonce": "n-789",
            }

    class _DB:
        async def commit(self):
            pass

    monkeypatch.setattr(mcp_oauth, "_resolve_session_user", _session_user)
    monkeypatch.setattr(mcp_oauth, "_load_client", _load)
    response = await mcp_oauth.authorize_session(request=_Req(), db=_DB())

    assert response.status_code == 200
    assert stored[0]["scope"] == "openid profile"
    assert stored[0]["nonce"] == "n-789"


async def test_the_password_form_forwards_scope_and_nonce(monkeypatch):
    import app.core.sso_helpers as sso_module

    stored = _capture_codes(monkeypatch)
    password = "Sup3rSecret1"
    user = _user()
    user.hashed_password = security.hash_password(password)
    user.mfa_enabled = False

    async def _load(*_a, **_kw):
        return _client()

    async def _branding(_db):
        return SimpleNamespace(name="TestApp")

    async def _sso(_db):
        return SimpleNamespace(local_auth_enabled=True)

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: user)

        async def commit(self):
            pass

    monkeypatch.setattr(mcp_oauth, "_load_client", _load)
    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _no_op)
    monkeypatch.setattr(mcp_oauth, "get_branding", _branding)
    monkeypatch.setattr(sso_module, "get_sso_config", _sso)
    response = await mcp_oauth.authorize_post(
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
        email=user.email,
        password=password,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="",
        code_challenge="challenge",
        code_challenge_method="S256",
        response_type="code",
        resource="",
        scope="openid email",
        nonce="n-321",
        mfa_token="",
        code="",
        db=_DB(),
    )

    assert isinstance(response, HTMLResponse)
    assert stored[0]["scope"] == "openid email"
    assert stored[0]["nonce"] == "n-321"


# ── Consent page ─────────────────────────────────────────────────────────────


def test_the_consent_page_carries_scope_and_nonce_to_both_paths():
    page = mcp_oauth._consent_html(client_name="OpenCRQ", scope="openid email", nonce='n"x')

    assert '<input type="hidden" name="scope" value="openid email">' in page
    assert '<input type="hidden" name="nonce" value="n&quot;x">' in page
    assert "scope: val('scope')" in page
    assert "nonce: val('nonce')" in page


def test_a_product_sign_in_without_session_goes_through_the_platform_login():
    """The in-page form checks a password without opening an XTM One session, so
    an ``openid`` request with no session is sent to the platform's sign-in page
    (which returns here through ``mcp_oauth_return``); MCP requests keep the form."""
    script = mcp_oauth._consent_script()

    assert "field.value === '1'" in script
    assert "if (token) { authorizeWithSession(); } else { signInToPlatform(); }" in script
    assert "if (isProductSignIn()) { signInToPlatform(); return; }" in script
    assert "sessionStorage.setItem('mcp_oauth_return', window.location.href)" in script
    assert "window.location.replace(BASE_PATH + '/login')" in script


@pytest.mark.parametrize(
    ("product_sign_in", "error", "mfa_token", "hidden"),
    [
        (True, "", "", True),
        (False, "", "", False),
        (True, "Invalid credentials", "", False),
        # The second-factor step asks for a code: it needs the card.
        (True, "", "challenge", False),
    ],
)
def test_only_a_product_sign_in_hides_the_consent_card(product_sign_in, error, mfa_token, hidden):
    page = mcp_oauth._consent_html(client_name="OpenCRQ", product_sign_in=product_sign_in, error=error, mfa_token=mfa_token)

    assert ('<div class="card" id="consent-card" style="display:none">' in page) is hidden
    assert ('<div class="card" id="consent-card">' in page) is not hidden
    assert ('<p class="session-info" id="signing-in">' in page) is hidden
    # Without JavaScript nothing would happen: the page says so, instead of "Signing you in".
    assert ("<noscript><style>#signing-in { display: none; }</style>" in page) is hidden
    assert ("JavaScript is required to sign in." in page) is hidden


# ── Scope bounds and product sign-in ─────────────────────────────────────────


def test_a_long_scope_without_openid_is_not_bounded():
    """Nothing changes without ``openid``: an MCP client's scope was never read."""
    mcp_oauth._check_oidc_params("x" * 600, "")
    with pytest.raises(HTTPException):
        mcp_oauth._check_oidc_params("openid " + "x" * 600, "")


async def _consent_page(monkeypatch, client, scope: str) -> str:
    async def _load(*_a, **_kw):
        return client

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr(mcp_oauth, "_load_client", _load)
    monkeypatch.setattr(mcp_oauth, "get_branding", _branding)
    response = await mcp_oauth.authorize_get(
        request=SimpleNamespace(),
        response_type="code",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge="challenge",
        code_challenge_method="S256",
        state="",
        resource="",
        scope=scope,
        nonce="n",
        db=None,
    )
    return response.body.decode()


@pytest.mark.parametrize(
    ("registered", "asked", "product_sign_in"),
    [
        (ALL_SCOPES, "openid email", "1"),
        # An MCP client sending scope=openid, not registered for it: keeps the in-page form.
        (None, "openid", ""),
        (ALL_SCOPES, "", ""),
    ],
)
async def test_the_server_decides_a_product_sign_in(monkeypatch, registered, asked, product_sign_in):
    page = await _consent_page(monkeypatch, _client(registered), asked)

    assert f'<input type="hidden" name="product_sign_in" value="{product_sign_in}">' in page


@pytest.mark.parametrize(
    ("declared", "kept"),
    [
        ("openid email profile xtm_role", None),
        ("mcp openid", "mcp"),
        (None, None),
        ("mcp", "mcp"),
    ],
)
async def test_a_self_registered_client_keeps_no_openid_connect_scope(monkeypatch, declared, kept):
    """Dynamic registration is open to anyone: whatever a client declares, no
    OpenID Connect scope is stored, so it never gets an ID token (a
    product's sign-in client comes from platform registration)."""
    import app.core.security_options as security_options

    added: list = []

    class _DB:
        def add(self, row):
            added.append(row)

        async def commit(self):
            pass

    monkeypatch.setattr(security_options, "security_option", lambda _name: True)
    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _no_op)
    monkeypatch.setattr(mcp_oauth, "_evict_oldest_clients_if_full", _no_op)
    body = mcp_oauth.ClientRegistrationRequest(client_name="Anyone", redirect_uris=[REDIRECT_URI], scope=declared)
    response = await mcp_oauth.register_client(request=None, body=body, db=_DB())

    assert response.status_code == 201
    assert added[0].scope == kept
