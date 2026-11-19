"""Authorizing a **company-managed** MCP server over OAuth.

A company-managed server used to be refused outright by
``start_mcp_oauth`` ("Company-managed MCP servers cannot use OAuth"), while
nothing stopped an admin from creating one in OAuth mode — so the detail page
showed "Authorization required. Click the button below…" with no button
anywhere and no way to ever connect the server.

The connection is legitimate: an OAuth token lives on the server row, exactly
like the shared API key of a header-authenticated company-managed server, so
one authorization serves every user of the platform. That is what makes it
admin-only, which is what these tests pin:

* a non-admin gets 403 and no authorization flow is ever started;
* an admin gets an authorization URL;
* the callback finds the server even though ``user_id`` is NULL on
  company-managed rows (the owner-match lookup can never hit).

The route functions are exercised directly with lightweight stubs — the
codebase has no FastAPI test client / DB session fixture (see
``test_mcp_dcr_protection.py`` for the same approach).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app.api import oauth_callbacks as callbacks_module
from app.api.oauth_callbacks import _handle_mcp_server_callback
from app.api.v1 import mcp_servers as mcp_servers_module
from app.api.v1.mcp_servers import start_mcp_oauth
from app.models.mcp_server import MCPServer
from app.services import token_refresh as token_refresh_module

AUTHORIZE_URL = "https://auth.example.com/oauth/authorize?client_id=abc"


class _StubDB:
    """Async DB stub returning queued ``scalar_one_or_none`` results in call
    order, capturing each statement's WHERE clause so a test can assert on
    the *shape* of the lookup without a real database."""

    def __init__(self, results: list[object] | None = None):
        self._results = list(results or [])
        self.where_texts: list[str] = []
        self.commits = 0

    async def execute(self, stmt):
        try:
            self.where_texts.append(
                str(stmt.whereclause) if stmt.whereclause is not None else ""
            )
        except Exception:  # pragma: no cover - defensive only
            self.where_texts.append("")
        value = self._results.pop(0) if self._results else None

        class _R:
            def scalar_one_or_none(self):
                return value

        return _R()

    async def commit(self):
        self.commits += 1


def _server(**overrides) -> SimpleNamespace:
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Company MCP",
        user_id=None,
        is_company_managed=True,
        auth_mode="oauth",
        registration_url=None,
        access_token_enc=None,
        refresh_token_enc=None,
        token_expires_at=None,
        scopes=None,
        oauth_status="disconnected",
    )
    for key, value in overrides.items():
        setattr(srv, key, value)
    return srv


def _user(*, is_admin: bool) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, full_name="Someone")


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), headers={}, url="/authorize"
    )


@pytest.fixture
def _no_audit(monkeypatch):
    async def _log_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(mcp_servers_module, "log_audit", _log_audit)


@pytest.fixture
def _oauth_stubs(monkeypatch):
    """Stub the two collaborators ``start_mcp_oauth`` imports at call time."""
    import app.integrations.oauth as oauth_module
    import app.services.mcp_oauth_discovery as discovery_module

    calls: dict[str, object] = {"ensure": 0, "built_for": None}

    async def _ensure(_db, _srv):
        calls["ensure"] = int(calls["ensure"]) + 1
        return discovery_module.MCPOAuthClientSetup()

    async def _build(srv, user_id, **_kw):
        calls["built_for"] = (srv, user_id)
        return AUTHORIZE_URL

    monkeypatch.setattr(discovery_module, "ensure_mcp_oauth_client", _ensure)
    monkeypatch.setattr(
        oauth_module, "build_authorize_url_for_mcp_server", _build, raising=True
    )
    return calls


async def _call_authorize(srv, user, db) -> dict:
    return await start_mcp_oauth(
        server_id=str(srv.id), request=_request(), user=user, db=db
    )


# ── the admin gate ──────────────────────────────────────────────────────


async def test_non_admin_cannot_authorize_a_company_managed_server(
    monkeypatch, _no_audit, _oauth_stubs
):
    """The token is shared platform-wide, so a plain user must be refused —
    and refused *before* any client registration or redirect is built."""
    srv = _server()

    async def _get(*_a, **_kw):
        return srv

    monkeypatch.setattr(mcp_servers_module, "get_user_mcp_server", _get)

    with pytest.raises(HTTPException) as exc:
        await _call_authorize(srv, _user(is_admin=False), _StubDB())

    assert exc.value.status_code == 403
    assert "admin" in exc.value.detail.lower()
    assert _oauth_stubs["ensure"] == 0, "no OAuth client registered for a refusal"
    assert _oauth_stubs["built_for"] is None


async def test_admin_gets_an_authorize_url_for_a_company_managed_server(
    monkeypatch, _no_audit, _oauth_stubs
):
    """The regression: an admin must be able to connect the server instead of
    reading a 400 that made the OAuth mode unusable."""
    srv = _server()
    admin = _user(is_admin=True)

    async def _get(*_a, **_kw):
        return srv

    monkeypatch.setattr(mcp_servers_module, "get_user_mcp_server", _get)

    result = await _call_authorize(srv, admin, _StubDB())

    assert result == {"authorize_url": AUTHORIZE_URL}
    assert _oauth_stubs["built_for"] == (srv, str(admin.id))


async def test_non_oauth_server_is_still_refused(monkeypatch, _no_audit, _oauth_stubs):
    """The auth-mode check must survive the gate rewrite: a header-based
    server has nothing to authorize."""
    srv = _server(auth_mode="headers")

    async def _get(*_a, **_kw):
        return srv

    monkeypatch.setattr(mcp_servers_module, "get_user_mcp_server", _get)

    with pytest.raises(HTTPException) as exc:
        await _call_authorize(srv, _user(is_admin=True), _StubDB())

    assert exc.value.status_code == 400
    assert "not configured for OAuth" in exc.value.detail


# ── the callback ────────────────────────────────────────────────────────


async def test_callback_finds_a_company_managed_server_despite_null_owner(
    monkeypatch,
):
    """``user_id`` is NULL on company-managed rows, so the owner-match lookup
    returns nothing — without the ``is_company_managed`` fallback the browser
    came back from the provider to a 404 and the token was thrown away."""
    # A mapped row: the callback flags ``oauth_status`` as written.
    srv = MCPServer(**vars(_server()))
    admin_id = uuid.uuid4()
    # First lookup (owner match) misses, second (company-managed) hits.
    db = _StubDB([None, srv])

    async def _exchange(_srv, _code, *, code_verifier=None):
        return {"access_token": "tok-abc", "refresh_token": "ref-abc"}

    async def _discover(_db, _srv):
        return None

    monkeypatch.setattr(
        callbacks_module, "exchange_code_for_tokens_with_mcp_server", _exchange
    )
    monkeypatch.setattr(
        callbacks_module, "_discover_tools_after_authorization", _discover
    )
    monkeypatch.setattr(callbacks_module, "encrypt_value", lambda v: f"enc:{v}")
    monkeypatch.setattr(token_refresh_module, "redis_client", AsyncMock(return_value=AsyncMock()))

    response = await _handle_mcp_server_callback(
        {
            "entity_id": str(srv.id),
            "user_id": str(admin_id),
            "entity_type": "mcp_server",
        },
        "the-code",
        db,
    )

    assert isinstance(response, RedirectResponse)
    assert f"/mcp-servers/{srv.id}?connected=true" in response.headers["location"]
    assert srv.oauth_status == "connected"
    assert srv.access_token_enc == "enc:tok-abc"
    assert srv.refresh_token_enc == "enc:ref-abc"
    assert "is_company_managed" in db.where_texts[1], (
        "the fallback lookup must select on the company-managed flag"
    )
