"""The allowed email domains restrict SSO account creation, OIDC and SAML alike.

``sso_config.allowed_domains`` restricted self-registration only, while the
SSO card read "Restricted to: <domains>": an SSO sign-in created an account for
whoever the identity provider authenticated, whatever the domain.
``_get_or_create_sso_user`` now refuses a new account outside the list with
:class:`SSOUserNotProvisionedError`, the not-provisioned refusal, and both
refusals decide through ``app.core.email_domains``.

Pinned here, through the helper and through the real callbacks (only the
identity provider, the SSO configuration and the database session are
stubbed; the configuration is a real ``ResolvedSSOConfig``):

* the refusal comes before any row is written: no account, no SAML group
  sync, no auto-admin promotion, no audit row and no session; the login page
  gets the ``not_provisioned`` marker and the log names the reason;
* an address at an allowed domain gets its account, case-insensitively, and
  without a list every domain does;
* an existing account keeps signing in whatever its domain, matched by SSO
  subject or linked by email: linking is not a creation;
* self-registration applies the same rule.
"""

from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app.api import auth_sso
from app.api.auth_sso import (
    SSOUserNotProvisionedError,
    _get_or_create_sso_user,
    oidc_callback,
    saml_acs,
)
from app.core.config import settings
from app.core.email_domains import email_domain_allowed, parse_allowed_domains
from app.core.sso_helpers import ResolvedSSOConfig
from app.models.audit_log import AuditLog
from app.models.user import User

FRONTEND = "https://app.example.com"
NOT_PROVISIONED = f"{FRONTEND}/login?sso_error=not_provisioned"
STORED_DOMAINS = " Example.com, ,corp.IO "
ALLOWED = ["example.com", "corp.io"]
OUTSIDER = "eve@elsewhere.example.net"
SUBJECT = "idp|eve"
SAML_EMAIL = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"


def _account(**overrides) -> User:
    """An active account at a domain outside :data:`ALLOWED`, linked to :data:`SUBJECT`."""
    fields = {
        "email": OUTSIDER,
        "full_name": "Eve (as stored)",
        "role": "user",
        "is_active": True,
        "sso_provider": "oidc",
        "sso_subject": SUBJECT,
    }
    return User(**{**fields, **overrides})


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        # No group has auto join on in these fakes (#1901).
        return []


class _FakeDb:
    """Enough session to drive the helper and the callbacks without Postgres.

    The two account lookups of ``_get_or_create_sso_user`` - by SSO subject,
    then by email - are told apart by their rendered WHERE clause. ``added``
    records every object the code tried to persist, audit rows included.
    """

    def __init__(self, *, by_subject: User | None = None, by_email: User | None = None):
        self.by_subject = by_subject
        self.by_email = by_email
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt):
        text = str(stmt)
        if "users.sso_subject =" in text:
            return _Result(self.by_subject)
        if "lower(users.email) =" in text:
            return _Result(self.by_email)
        return _Result(None)

    async def scalar(self, stmt):
        return 1  # not the first account, so ``_determine_role`` returns "user"

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        raise AssertionError(f"nothing is deleted on an SSO sign-in: {obj!r}")

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    @property
    def created_users(self) -> list[User]:
        return [o for o in self.added if isinstance(o, User)]

    @property
    def audit_rows(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


def _assert_refused(response, *, status_code: int) -> None:
    assert isinstance(response, RedirectResponse)
    assert response.status_code == status_code
    assert response.headers["location"] == NOT_PROVISIONED


def _refusal_logged(caplog, provider_label: str) -> bool:
    return any(
        f"{provider_label} sign-in refused" in record.getMessage()
        and f"{OUTSIDER} and its domain is not one of the allowed email domains (example.com, corp.io)"
        in record.getMessage()
        for record in caplog.records
    )


@pytest.fixture(autouse=True)
def _frontend(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", FRONTEND)
    # The XTM Hub gate is off unless a test turns it on.
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")


# ===================================================================
# One rule: parsed and matched once
# ===================================================================


def test_the_stored_list_is_parsed_once_for_every_reader():
    assert parse_allowed_domains(STORED_DOMAINS) == ALLOWED
    assert parse_allowed_domains("") == parse_allowed_domains(None) == parse_allowed_domains(" , ") == []
    assert ResolvedSSOConfig(allowed_domains=STORED_DOMAINS).allowed_domain_list == ALLOWED


@pytest.mark.parametrize(
    ("email", "allowed"),
    [
        ("alice@example.com", True),
        (" Alice@EXAMPLE.com ", True),
        ("bob@corp.io", True),
        # Exact: a subdomain, or a name that merely contains an allowed one, is another domain.
        ("eve@eu.example.com", False),
        ("eve@example.com.elsewhere.example.net", False),
        ("eve@notexample.com", False),
        # The domain follows the last @.
        ("eve@example.com@elsewhere.example.net", False),
        # Not an address: a bare user name some identity providers assert.
        ("example.com", False),
        ("@example.com", False),
        ("", False),
    ],
)
def test_an_address_is_matched_on_its_domain_exactly_and_case_insensitively(email, allowed):
    assert email_domain_allowed(email, ALLOWED) is allowed


@pytest.mark.parametrize("email", [OUTSIDER, "not-an-address", ""])
def test_without_a_list_every_address_is_allowed(email):
    assert email_domain_allowed(email, []) is True


# ===================================================================
# Account resolution
# ===================================================================


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_a_new_account_outside_the_allowed_domains_is_refused_before_any_row_is_written(provider):
    db = _FakeDb()

    with pytest.raises(SSOUserNotProvisionedError) as excinfo:
        await _get_or_create_sso_user(
            db,
            OUTSIDER,
            "Eve",
            provider,
            SUBJECT,
            auto_admin=True,  # would promote the account it refuses to create
            auto_create=True,
            allowed_domains=ALLOWED,
        )

    assert db.added == []
    # The log line the operator acts on: who, through which method, and why.
    assert str(excinfo.value) == (
        f"No existing account for {OUTSIDER} and its domain is not one of the allowed "
        "email domains (example.com, corp.io)"
    )
    assert excinfo.value.sso_provider == provider


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_a_new_account_at_an_allowed_domain_is_created(provider):
    db = _FakeDb()

    user = await _get_or_create_sso_user(
        db, "Carol@CORP.io", "Carol", provider, SUBJECT, allowed_domains=ALLOWED
    )

    assert db.created_users == [user]
    assert (user.sso_provider, user.sso_subject) == (provider, SUBJECT)


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_without_allowed_domains_every_domain_gets_an_account(provider):
    db = _FakeDb()

    user = await _get_or_create_sso_user(db, OUTSIDER, "Eve", provider, SUBJECT, allowed_domains=[])

    assert db.created_users == [user]


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_an_existing_account_outside_the_allowed_domains_still_signs_in(provider):
    existing = _account(sso_provider=provider)
    db = _FakeDb(by_subject=existing)

    user = await _get_or_create_sso_user(
        db, OUTSIDER, "Eve", provider, SUBJECT, auto_admin=True, allowed_domains=ALLOWED
    )

    assert user is existing
    # Auto-admin applies at every sign-in, existing accounts included.
    assert user.role == "admin"
    assert db.added == []


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_an_existing_account_outside_the_allowed_domains_is_still_linked_by_email(provider):
    """Linking is not a creation: a local account at another domain gets its SSO identity."""
    existing = _account(sso_provider=None, sso_subject=None)
    db = _FakeDb(by_email=existing)

    user = await _get_or_create_sso_user(db, OUTSIDER, "Eve", provider, SUBJECT, allowed_domains=ALLOWED)

    assert user is existing
    assert (user.sso_provider, user.sso_subject) == (provider, SUBJECT)
    assert db.added == []


async def test_with_creation_off_the_refusal_keeps_its_own_reason():
    with pytest.raises(SSOUserNotProvisionedError) as excinfo:
        await _get_or_create_sso_user(
            _FakeDb(), OUTSIDER, "Eve", "saml", SUBJECT, auto_create=False, allowed_domains=ALLOWED
        )

    assert str(excinfo.value) == (
        f"No existing account for {OUTSIDER} and on-the-fly user creation is disabled for saml"
    )


# ===================================================================
# OIDC callback
# ===================================================================


@pytest.fixture
def oidc(monkeypatch):
    """Real callback, stubbed provider: claims arrive via ``token_resp["userinfo"]``."""
    import app.core.sso_helpers as sso_helpers

    stack = SimpleNamespace(
        cfg=ResolvedSSOConfig(oidc_enabled=True, oidc_auto_create_users=True, allowed_domains=STORED_DOMAINS),
        claims={"email": OUTSIDER, "name": "Eve (from the IdP)", "sub": SUBJECT},
    )

    async def _cfg(db):
        return stack.cfg

    async def _disco(_cfg):
        return {"token_endpoint": "https://idp.example.com/token"}

    class _Client:
        async def fetch_token(self, *a, **kw):
            return {"access_token": "at", "userinfo": dict(stack.claims)}

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_oidc_discovery_from_cfg", _disco)
    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", lambda _cfg: _Client())
    return stack


async def test_oidc_refuses_a_new_account_outside_the_allowed_domains(oidc, caplog):
    oidc.cfg.oidc_auto_admin_new_users = True
    db = _FakeDb()

    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        response = await oidc_callback(code="x", db=db)

    _assert_refused(response, status_code=307)
    assert db.added == []  # no account, no audit row claiming a sign-in
    assert db.commits == 0
    assert _refusal_logged(caplog, "OIDC")


@pytest.mark.parametrize("email", ["dana@example.com", "Dana@CORP.io"])
async def test_oidc_creates_an_account_at_an_allowed_domain(oidc, email):
    oidc.claims["email"] = email
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert "#token=" in response.headers["location"]
    assert [u.email.lower() for u in db.created_users] == [email.lower()]
    assert [row.action for row in db.audit_rows] == ["login"]


async def test_oidc_without_allowed_domains_creates_an_account_at_any_domain(oidc):
    oidc.cfg.allowed_domains = ""
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert "#token=" in response.headers["location"]
    assert [u.email for u in db.created_users] == [OUTSIDER]


@pytest.mark.parametrize("matched_by", ["subject", "email"])
async def test_oidc_signs_in_an_existing_account_outside_the_allowed_domains(oidc, matched_by):
    if matched_by == "subject":
        existing = _account()
        db = _FakeDb(by_subject=existing)
    else:
        existing = _account(sso_provider=None, sso_subject=None)
        db = _FakeDb(by_email=existing)

    response = await oidc_callback(code="x", db=db)

    assert "#token=" in response.headers["location"]
    assert db.created_users == []
    assert (existing.sso_provider, existing.sso_subject) == ("oidc", SUBJECT)
    assert [row.action for row in db.audit_rows] == ["login"]


async def test_an_identity_the_xtm_hub_entitles_gets_no_account_outside_the_allowed_domains(oidc, monkeypatch):
    """The Hub decides who may sign in through OIDC; the domains still decide who gets a new account."""
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "Admin")
    monkeypatch.setattr(settings, "platform_id", "xtmone-admin")
    oidc.claims["xtmone-admin_groups"] = ["Admin"]
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    _assert_refused(response, status_code=307)
    assert db.added == []


# ===================================================================
# SAML Assertion Consumer Service
# ===================================================================


class _Request:
    """The parts of a Starlette request the ACS and the audit trail read."""

    def __init__(self):
        self.query_params: dict = {}
        self.headers: dict = {}
        self.client = SimpleNamespace(host="203.0.113.7")

    async def form(self):
        return {"SAMLResponse": "opaque"}


@pytest.fixture
def saml(monkeypatch):
    """Real ACS, stubbed python3-saml: a validated assertion with these attributes."""
    import app.core.sso_helpers as sso_helpers

    stack = SimpleNamespace(
        cfg=ResolvedSSOConfig(
            saml_enabled=True,
            saml_auto_create_users=True,
            saml_groups_mapping_enabled=True,
            allowed_domains=STORED_DOMAINS,
        ),
        attrs={SAML_EMAIL: [OUTSIDER], "groups": ["Security Team"]},
        synced=[],
    )

    class _Auth:
        def __init__(self, req, saml_settings):
            pass

        def process_response(self):
            pass

        def get_errors(self):
            return []

        def get_last_error_reason(self):
            return None

        def is_authenticated(self):
            return True

        def get_attributes(self):
            return stack.attrs

        def get_nameid(self):
            return SUBJECT

    fake_module = types.ModuleType("onelogin.saml2.auth")
    fake_module.OneLogin_Saml2_Auth = _Auth
    monkeypatch.setitem(sys.modules, "onelogin", types.ModuleType("onelogin"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2", types.ModuleType("onelogin.saml2"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2.auth", fake_module)

    async def _cfg(db):
        return stack.cfg

    async def _saml_settings(_cfg):
        return {}

    async def _sync(db, user, group_names, auto_create):
        stack.synced.append((user, list(group_names)))
        return False

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_build_saml_settings_from_cfg", _saml_settings)
    monkeypatch.setattr(auth_sso, "_sync_user_groups", _sync)
    return stack


async def test_saml_refuses_a_new_account_outside_the_allowed_domains(saml, caplog):
    saml.cfg.saml_auto_admin_new_users = True
    db = _FakeDb()

    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        response = await saml_acs(_Request(), db=db)

    # 303 so the browser follows the POST-back with a GET.
    _assert_refused(response, status_code=303)
    assert saml.synced == []  # no group sync for an account that was never created
    assert db.added == []
    assert db.commits == 0
    assert _refusal_logged(caplog, "SAML")


async def test_saml_creates_an_account_at_an_allowed_domain_and_syncs_its_groups(saml):
    saml.attrs[SAML_EMAIL] = ["dana@corp.io"]
    db = _FakeDb()

    response = await saml_acs(_Request(), db=db)

    assert response.status_code == 303
    assert "#token=" in response.headers["location"]
    assert [u.email for u in db.created_users] == ["dana@corp.io"]
    assert saml.synced == [(db.created_users[0], ["Security Team"])]


@pytest.mark.parametrize("matched_by", ["subject", "email"])
async def test_saml_signs_in_an_existing_account_outside_the_allowed_domains(saml, matched_by):
    if matched_by == "subject":
        existing = _account(sso_provider="saml")
        db = _FakeDb(by_subject=existing)
    else:
        existing = _account(sso_provider=None, sso_subject=None)
        db = _FakeDb(by_email=existing)

    response = await saml_acs(_Request(), db=db)

    assert response.status_code == 303
    assert "#token=" in response.headers["location"]
    assert db.created_users == []
    assert (existing.sso_provider, existing.sso_subject) == ("saml", SUBJECT)
    assert saml.synced == [(existing, ["Security Team"])]
    assert [row.action for row in db.audit_rows] == ["login"]


# ===================================================================
# Self-registration: the same rule
# ===================================================================


class _RegistrationDb:
    """``register``'s reads: the account count, then the exists check, which finds an account."""

    def __init__(self):
        self.lookups = 0

    async def scalar(self, _stmt):
        return 5

    async def execute(self, _stmt):
        self.lookups += 1
        return _Result(object())


@pytest.fixture
def registration(monkeypatch):
    import app.core.sso_helpers as sso_helpers

    cfg = ResolvedSSOConfig(local_auth_enabled=True, registration_enabled=True, allowed_domains=STORED_DOMAINS)

    async def _cfg(_db):
        return cfg

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    return cfg


async def _register(email: str) -> tuple[HTTPException, _RegistrationDb]:
    from app.api.v1.auth import RegisterRequest, register

    db = _RegistrationDb()
    body = RegisterRequest(email=email, password="Sup3rSecret", full_name="Someone")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    with pytest.raises(HTTPException) as excinfo:
        await register(body=body, request=request, db=db)
    return excinfo.value, db


async def test_self_registration_refuses_an_address_outside_the_allowed_domains(registration):
    refused, db = await _register(OUTSIDER)

    assert (refused.status_code, refused.detail) == (403, "Registration restricted to: example.com, corp.io")
    assert db.lookups == 0


async def test_self_registration_matches_the_domain_case_insensitively(registration):
    # Past the domain check, the exists check answers: the address is taken.
    refused, db = await _register("Foo@EXAMPLE.com")

    assert (refused.status_code, refused.detail) == (409, "Email already registered")
    assert db.lookups == 1
