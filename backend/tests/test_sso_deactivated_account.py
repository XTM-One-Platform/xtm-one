"""An SSO sign-in of a deactivated account is refused at the callback.

An OIDC or SAML sign-in of an account an
administrator had deactivated used to COMPLETE: the callback linked and updated
the account, synced its SAML groups, applied the XTM Hub role and handed back a
session token that every request then refused. The refusal now happens inside
``_get_or_create_sso_user``, before anything on the account is touched and
before any token is minted, and the login page receives the
``account_deactivated`` marker.

Pinned here, for both providers and through the real callbacks (only the
identity provider, the SSO configuration and the database session are stubbed):

* no session: the redirect carries no ``token`` / ``mfa_token`` fragment and
  sets no cookie;
* no change: the account's profile, SSO link, role and activation flag are
  exactly as they were, its SAML groups are not synced, and no second account
  is created alongside it;
* a durable ``login_refused`` audit row attributed to the account;
* active accounts, and on-the-fly provisioning of a new address, are unchanged.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse

from app.api import auth_sso
from app.api.auth_sso import (
    SSOAccountDeactivatedError,
    _get_or_create_sso_user,
    oidc_callback,
    saml_acs,
)
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.user import User

EMAIL = "dana@example.com"
SUBJECT = "idp|dana"
FRONTEND = "https://app.example.com"
REFUSED = f"{FRONTEND}/login?sso_error=account_deactivated"

#: Everything an SSO sign-in could write on an existing account.
_ACCOUNT_FIELDS = (
    "email",
    "full_name",
    "title",
    "sso_provider",
    "sso_subject",
    "role",
    "is_active",
    "default_agent_id",
)


def _account(*, active: bool, linked: bool = True, role: str = "user") -> User:
    """An account as the database returns it (``is_active`` is always a bool)."""
    return User(
        email=EMAIL,
        full_name="Dana (as stored)",
        title="Stored title",
        role=role,
        is_active=active,
        sso_provider="oidc" if linked else None,
        sso_subject=SUBJECT if linked else None,
    )


def _snapshot(user: User) -> dict:
    return {field: getattr(user, field) for field in _ACCOUNT_FIELDS}


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        # No group has auto join on in these fakes (#1901).
        return []


class _FakeDb:
    """Enough session to drive the callbacks without Postgres.

    The two account lookups of ``_get_or_create_sso_user`` - by SSO subject,
    then by email - are told apart by their rendered WHERE clause, so each
    branch is exercised on its own. ``added`` records every object the code
    tried to persist; ``deleted`` every object it tried to remove.
    """

    def __init__(self, *, by_subject: User | None = None, by_email: User | None = None):
        self.by_subject = by_subject
        self.by_email = by_email
        self.added: list = []
        self.deleted: list = []
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
        self.deleted.append(obj)

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


def _assert_no_session(response) -> None:
    location = response.headers["location"]
    assert location == REFUSED
    assert "token=" not in location
    assert "set-cookie" not in response.headers  # case-insensitive lookup


def _assert_refusal_audited(db: _FakeDb, user: User, provider: str) -> AuditLog:
    assert [row.action for row in db.audit_rows] == ["login_refused"]
    row = db.audit_rows[0]
    assert row.user_email == user.email
    assert row.entity_type == "user"
    assert "deactivated" in row.summary
    assert row.details == {"login_method": provider, "reason": "account_deactivated"}
    assert db.commits >= 1
    return row


@pytest.fixture(autouse=True)
def _frontend(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", FRONTEND)
    # The XTM Hub gate is off unless a test turns it on.
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")


# ===================================================================
# Account resolution: refused before anything is changed
# ===================================================================


@pytest.mark.parametrize("provider", ["oidc", "saml"])
async def test_a_deactivated_account_matched_by_subject_is_refused_untouched(provider):
    existing = _account(active=False)
    existing.sso_provider = provider
    before = _snapshot(existing)
    db = _FakeDb(by_subject=existing)

    with pytest.raises(SSOAccountDeactivatedError) as excinfo:
        await _get_or_create_sso_user(
            db,
            EMAIL,
            "Dana (from the IdP)",
            provider,
            SUBJECT,
            title="IdP title",
            auto_admin=True,  # would promote an active account
            auto_create=True,  # and must not create a second one
        )

    assert excinfo.value.user is existing
    assert _snapshot(existing) == before
    assert db.added == []


async def test_a_deactivated_account_found_by_email_is_not_linked():
    existing = _account(active=False, linked=False)
    before = _snapshot(existing)
    db = _FakeDb(by_email=existing)

    with pytest.raises(SSOAccountDeactivatedError):
        await _get_or_create_sso_user(
            db, EMAIL, "Dana (from the IdP)", "saml", SUBJECT, auto_create=True
        )

    assert _snapshot(existing) == before
    assert existing.sso_subject is None
    assert db.added == []


async def test_an_active_account_found_by_email_is_still_linked():
    """The control: linking and profile updates are unchanged for active accounts."""
    existing = _account(active=True, linked=False)
    db = _FakeDb(by_email=existing)

    user = await _get_or_create_sso_user(
        db, EMAIL, "Dana (from the IdP)", "saml", SUBJECT
    )

    assert user is existing
    assert (user.sso_provider, user.sso_subject) == ("saml", SUBJECT)
    assert user.full_name == "Dana (from the IdP)"
    assert db.added == []


# ===================================================================
# OIDC callback
# ===================================================================


@pytest.fixture
def oidc(monkeypatch):
    """Real callback, stubbed provider: claims arrive via ``token_resp["userinfo"]``."""
    import app.core.sso_helpers as sso_helpers

    stack = SimpleNamespace(
        cfg=SimpleNamespace(
            oidc_enabled=True,
            oidc_auto_admin_new_users=False,
            oidc_auto_create_users=True,
            allowed_domain_list=[],
        ),
        claims={
            "email": EMAIL,
            "name": "Dana (from the IdP)",
            "sub": SUBJECT,
            "jobTitle": "IdP title",
        },
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


@pytest.mark.parametrize("matched_by", ["subject", "email"])
async def test_oidc_refuses_a_deactivated_account_before_any_token(oidc, matched_by):
    existing = _account(active=False, linked=matched_by == "subject")
    before = _snapshot(existing)
    db = (
        _FakeDb(by_subject=existing)
        if matched_by == "subject"
        else _FakeDb(by_email=existing)
    )

    response = await oidc_callback(code="x", db=db)

    assert isinstance(response, RedirectResponse)
    _assert_no_session(response)
    assert db.created_users == []
    assert db.deleted == []
    assert _snapshot(existing) == before
    _assert_refusal_audited(db, existing, "oidc")


async def test_oidc_refusal_owes_no_second_factor_either(oidc, monkeypatch):
    """An enrolled account gets no challenge token: there is nothing to complete."""
    import app.services.mfa as mfa

    monkeypatch.setattr(mfa, "challenge_required", lambda user: True)
    monkeypatch.setattr(mfa, "create_challenge_token", lambda *a, **kw: "challenge")
    db = _FakeDb(by_subject=_account(active=False))

    response = await oidc_callback(code="x", db=db)

    _assert_no_session(response)
    assert "mfa_token" not in response.headers["location"]


async def test_oidc_refusal_hands_nothing_to_a_desktop_app(oidc):
    """A sign-in started from XTM One Desktop gets no handoff to complete."""
    db = _FakeDb(by_subject=_account(active=False))

    response = await oidc_callback(code="x", state="desktop:" + "d" * 43, db=db)

    _assert_no_session(response)
    assert "desktop_login" not in response.headers["location"]


async def test_oidc_refusal_skips_the_hub_role_sync(oidc, monkeypatch):
    """An entitled Hub identity is still refused, and its role is left alone."""
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "Admin")
    monkeypatch.setattr(settings, "platform_id", "xtmone-admin")
    oidc.claims["xtmone-admin_groups"] = ["Admin"]
    existing = _account(active=False, role="user")
    db = _FakeDb(by_subject=existing)

    response = await oidc_callback(code="x", db=db)

    _assert_no_session(response)
    assert existing.role == "user"
    assert [row.action for row in db.audit_rows] == ["login_refused"]


async def test_oidc_active_account_signs_in_as_before(oidc):
    existing = _account(active=True)
    db = _FakeDb(by_subject=existing)

    response = await oidc_callback(code="x", db=db)

    assert "#token=" in response.headers["location"]
    assert existing.full_name == "Dana (from the IdP)"
    assert [row.action for row in db.audit_rows] == ["login"]


async def test_oidc_new_address_is_still_provisioned(oidc):
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert "#token=" in response.headers["location"]
    assert [u.email for u in db.created_users] == [EMAIL]
    assert db.created_users[0].sso_provider == "oidc"


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
        return {"SAMLResponse": "opaque", "RelayState": "desktop:" + "d" * 43}


@pytest.fixture
def saml(monkeypatch):
    """Real ACS, stubbed python3-saml: a validated assertion with these attributes."""
    import app.core.sso_helpers as sso_helpers

    stack = SimpleNamespace(
        cfg=SimpleNamespace(
            saml_enabled=True,
            saml_auto_admin_new_users=False,
            saml_auto_create_users=True,
            saml_groups_mapping_enabled=True,
            saml_groups_auto_create=True,
            allowed_domain_list=[],
        ),
        attrs={
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": [EMAIL],
            "http://schemas.microsoft.com/identity/claims/displayname": [
                "Dana (from the IdP)"
            ],
            "groups": ["Security Team"],
        },
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

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_build_saml_settings_from_cfg", _saml_settings)
    monkeypatch.setattr(auth_sso, "_sync_user_groups", _sync)
    return stack


@pytest.mark.parametrize("matched_by", ["subject", "email"])
async def test_saml_refuses_a_deactivated_account_before_any_token(saml, matched_by):
    existing = _account(active=False, linked=matched_by == "subject")
    existing.sso_provider = "saml" if matched_by == "subject" else None
    before = _snapshot(existing)
    db = (
        _FakeDb(by_subject=existing)
        if matched_by == "subject"
        else _FakeDb(by_email=existing)
    )

    response = await saml_acs(_Request(), db=db)

    # 303 so the browser follows the POST-back with a GET.
    assert response.status_code == 303
    _assert_no_session(response)
    assert saml.synced == []
    assert db.created_users == []
    assert db.deleted == []
    assert _snapshot(existing) == before
    row = _assert_refusal_audited(db, existing, "saml")
    assert row.ip_address == "203.0.113.7"


async def test_saml_active_account_signs_in_and_syncs_groups(saml):
    existing = _account(active=True)
    existing.sso_provider = "saml"
    db = _FakeDb(by_subject=existing)

    response = await saml_acs(_Request(), db=db)

    assert response.status_code == 303
    assert "#token=" in response.headers["location"]
    assert saml.synced == [(existing, ["Security Team"])]
    assert [row.action for row in db.audit_rows] == ["login"]


async def test_saml_new_address_is_still_provisioned(saml):
    db = _FakeDb()

    response = await saml_acs(_Request(), db=db)

    assert "#token=" in response.headers["location"]
    assert [u.email for u in db.created_users] == [EMAIL]
    assert saml.synced == [(db.created_users[0], ["Security Team"])]


# ===================================================================
# The login page says it, in every catalog
# ===================================================================


def test_the_login_page_explains_the_marker_in_every_language():
    """The marker the callbacks emit has a message, translated four times.

    ``validate-i18n.mjs`` only catches orphans and bad ICU, not a key missing
    from a translation (the page would fall back to English silently).
    """
    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    message = "This account has been deactivated. Contact your administrator."
    login = (root / "pages" / "Login.tsx").read_text(encoding="utf-8")
    assert f'account_deactivated: t_i18n("{message}")' in login

    catalogs = {
        lang: json.loads((root / "i18n" / "lang" / f"{lang}.json").read_text(encoding="utf-8"))
        for lang in ("en", "fr", "es", "ja")
    }
    assert catalogs["en"][message] == message
    for lang in ("fr", "es", "ja"):
        assert catalogs[lang].get(message), f"{lang} lacks the deactivated message"
        assert catalogs[lang][message] != message, f"{lang} is not translated"
