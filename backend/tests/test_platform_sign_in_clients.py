"""A registered platform's sign-in client.

Platform registration, authenticated by the registration token, is the one way
a product gets an OpenID Connect client: it declares its callbacks, XTM One
keeps a public client for it and answers its id. Never with the published
default token, never with callbacks off the platform's origin, never evicted
by the bound on dynamic clients, removed on unregistration.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import mcp_oauth, platform
from app.services import platform_sign_in_clients as clients

PLATFORM_URL = "https://opencrq.example.com"
CALLBACK = f"{PLATFORM_URL}/auth/xtm-one/callback"


class _DB:
    """Holds at most one client row; records what is added and executed."""

    def __init__(self, row=None):
        self.row = row
        self.added: list = []
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        row = self.row
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, obj):
        self.added.append(obj)


# ── Callbacks ────────────────────────────────────────────────────────────────


def test_callbacks_on_the_platform_origin_are_accepted():
    clients.check_redirect_uris(PLATFORM_URL, [CALLBACK, f"{PLATFORM_URL}/other"])
    clients.check_redirect_uris("http://127.0.0.1:3999", ["http://127.0.0.1:3999/callback"])


@pytest.mark.parametrize(
    "callback",
    [
        "https://evil.example.com/callback",
        "https://opencrq.example.com:8443/callback",
        f"{CALLBACK}#fragment",
    ],
)
def test_a_callback_off_the_platform_origin_is_refused(callback):
    with pytest.raises(HTTPException) as exc:
        clients.check_redirect_uris(PLATFORM_URL, [callback])
    assert exc.value.status_code == 400


def test_a_plain_http_callback_off_loopback_is_refused():
    with pytest.raises(HTTPException):
        clients.check_redirect_uris("http://opencrq.internal", ["http://opencrq.internal/callback"])


def test_the_default_port_spelled_or_not_is_the_same_origin():
    clients.check_redirect_uris(PLATFORM_URL, ["https://opencrq.example.com:443/auth/xtm-one/callback"])
    clients.check_redirect_uris("https://opencrq.example.com:443", [CALLBACK])


def test_a_callback_carrying_credentials_is_refused():
    with pytest.raises(HTTPException):
        clients.check_redirect_uris(PLATFORM_URL, ["https://user@opencrq.example.com/callback"])


@pytest.mark.parametrize("identifier", ["opencrq", "opencti", "e2e-fake-product-a", "a" * 50])
def test_a_platform_identifier_fit_for_a_client_id_is_accepted(identifier):
    clients.check_identifier(identifier)


@pytest.mark.parametrize("identifier", ["", "OpenCRQ", "open crq", "-opencrq", "a" * 51, "x/../y"])
def test_a_platform_identifier_unfit_for_a_client_id_is_refused(identifier):
    with pytest.raises(HTTPException) as exc:
        clients.check_identifier(identifier)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(("client_id", "registration_status", "active"), [
    ("xtm-platform-opencrq", "active", True),
    ("xtm-platform-opencrq", "unregistered", False),
    ("xtm-platform-opencrq", None, False),  # purged: no registration row
    ("mcp-0123456789abcdef", "active", False),
])
async def test_only_an_active_registration_keeps_its_client_signing_in(client_id, registration_status, active):
    db = _DB(registration_status)

    assert await clients.is_active_platform_client(db, client_id) is active


# ── The client ───────────────────────────────────────────────────────────────


async def test_registration_creates_a_public_sign_in_client():
    db = _DB()
    client_id = await clients.sync_platform_sign_in_client(
        db, platform_identifier="opencrq", platform_title="OpenCRQ", redirect_uris=[CALLBACK]
    )

    assert client_id == "xtm-platform-opencrq"
    (row,) = db.added
    assert row.client_id == client_id
    assert row.redirect_uris == [CALLBACK]
    assert row.scope == "openid email profile xtm_role"
    assert row.token_endpoint_auth_method == "none"
    assert row.client_secret_hash is None
    # The OAuth server grants it the platform role, unlike a self-registered client.
    assert "xtm_role" in mcp_oauth._granted_oidc_scope("openid xtm_role", row)


async def test_a_heartbeat_keeps_the_client_in_step():
    # An older row still lists the refresh_token grant.
    row = SimpleNamespace(
        redirect_uris=[CALLBACK],
        client_name="OpenCRQ",
        scope="openid email profile xtm_role",
        grant_types=["authorization_code", "refresh_token"],
    )
    db = _DB(row)
    await clients.sync_platform_sign_in_client(
        db, platform_identifier="opencrq", platform_title="OpenCRQ 2", redirect_uris=[f"{PLATFORM_URL}/new"]
    )

    assert db.added == []
    assert row.redirect_uris == [f"{PLATFORM_URL}/new"]
    assert row.client_name == "OpenCRQ 2"
    assert row.grant_types == ["authorization_code"]


async def test_platform_clients_are_never_evicted():
    """The bound on dynamic clients evicts the oldest rows: never a platform's."""
    statements: list = []

    class _EvictDB:
        async def execute(self, stmt, *_args):
            statements.append(stmt)
            return SimpleNamespace(rowcount=0)

        async def scalar(self, stmt, *_args):
            statements.append(stmt)
            return 10_000

    await mcp_oauth._evict_oldest_clients_if_full(_EvictDB())
    # Both the count and the pick of the oldest rows leave platform clients out.
    from sqlalchemy.dialects import postgresql

    rendered = [
        str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for s in statements
        if "mcp_oauth_clients" in str(s)
    ]
    excluded = [sql for sql in rendered if "client_id NOT LIKE 'xtm-platform-'" in sql]
    assert len(excluded) == 2, rendered


# ── Registration ─────────────────────────────────────────────────────────────


def _body(redirect_uris):
    return platform.RegisterRequest(
        platform_identifier="opencrq", platform_url=PLATFORM_URL, sign_in_redirect_uris=redirect_uris
    )


async def test_a_configured_token_gets_a_sign_in_client(monkeypatch):
    monkeypatch.setattr(platform, "registration_token_state", lambda: "configured")
    db = _DB()

    assert await platform._sync_sign_in_client(db, _body([CALLBACK]), "OpenCRQ") == "xtm-platform-opencrq"
    assert db.added[0].client_id == "xtm-platform-opencrq"


async def test_the_published_default_token_gets_no_sign_in_client(monkeypatch):
    """Anyone knows that token: a client it created would be no more trusted
    than a self-registered one."""
    monkeypatch.setattr(platform, "registration_token_state", lambda: "published_default")
    db = _DB()

    assert await platform._sync_sign_in_client(db, _body([CALLBACK]), "OpenCRQ") is None
    assert db.added == []


async def test_a_platform_that_does_not_sign_users_in_costs_no_query(monkeypatch):
    """Every heartbeat of today's products: no field, no query, nothing changes."""
    monkeypatch.setattr(platform, "registration_token_state", lambda: "configured")
    db = _DB()

    assert await platform._sync_sign_in_client(db, _body(None), "OpenCRQ") is None
    assert db.statements == []
    assert db.added == []


async def test_purging_a_registration_removes_its_sign_in_client(monkeypatch):
    """The admin purge is the second way a platform goes: its client goes too."""
    import app.services.audit as audit
    import app.services.registered_platform_origins as origins

    registration = SimpleNamespace(
        id="5e8f1c64-1b1a-4c7e-9d4e-000000005706",
        platform_identifier="opencrq",
        platform_title="OpenCRQ",
        platform_url=PLATFORM_URL,
    )
    deleted: list = []

    class _PurgeDB:
        def __init__(self):
            self.statements: list = []
            self.results = [registration, []]

        async def execute(self, stmt):
            self.statements.append(stmt)
            value = self.results.pop(0) if self.results else None
            return SimpleNamespace(
                scalar_one_or_none=lambda: value,
                scalars=lambda: SimpleNamespace(all=lambda: value or []),
            )

        async def delete(self, obj):
            deleted.append(obj)

        async def commit(self):
            pass

    async def _no_op(*_a, **_kw):
        return None

    monkeypatch.setattr(audit, "log_audit", _no_op)
    monkeypatch.setattr(origins, "refresh_registered_platform_origins", _no_op)
    db = _PurgeDB()
    await platform.purge_registration(
        registration_id=registration.id, request=None, user=SimpleNamespace(email="admin@example.com"), db=db
    )

    assert registration in deleted
    assert any("DELETE FROM mcp_oauth_clients" in str(stmt) for stmt in db.statements)


async def test_unregistering_removes_the_sign_in_client():
    db = _DB()
    await clients.remove_platform_sign_in_client(db, "opencrq")

    (stmt,) = db.statements
    assert "DELETE FROM mcp_oauth_clients" in str(stmt)
