"""Tests for the SSO "On-the-fly user creation" toggle.

``_get_or_create_sso_user`` gained an ``auto_create`` argument: when the
toggle is off for an SSO method, an identity that matches no existing
account must be refused with :class:`SSOUserNotProvisionedError` BEFORE any
user row is written — while both existing-account paths (matched by SSO
subject, or linked by email) keep signing in exactly as before.

The security property pinned here is *placement*: the refusal happens before
``db.add`` is ever called, so a refused sign-in leaves no account behind by
construction rather than by compensation. A create-then-check-then-delete
implementation would pass a test that only counted rows at the end; the
``added`` assertions here fail it.
"""

from __future__ import annotations

import pytest

from app.api.auth_sso import (
    SSOUserNotProvisionedError,
    _get_or_create_sso_user,
    _sso_error_redirect,
)
from app.core.config import settings
from app.models.user import User


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    """Enough session to drive ``_get_or_create_sso_user`` without Postgres.

    The helper issues two user lookups — by (sso_provider, sso_subject), then
    by email — distinguished here by the rendered WHERE clause. ``added`` is
    what matters: it records every object the helper tried to persist.
    """

    def __init__(self, by_subject: User | None = None, by_email: User | None = None):
        self.by_subject = by_subject
        self.by_email = by_email
        self.added: list = []

    async def execute(self, stmt):
        # Match on the rendered WHERE clause, not the column list — every
        # SELECT of ``User`` names ``sso_provider`` among its columns.
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

    async def flush(self):
        pass


async def test_unknown_identity_is_refused_before_any_row_is_written():
    """The toggle's contract: refusal happens before ``db.add``, not after."""
    db = _FakeDb()

    with pytest.raises(SSOUserNotProvisionedError):
        await _get_or_create_sso_user(
            db,
            "nobody@example.com",
            "No Body",
            "oidc",
            "subject-1",
            auto_create=False,
        )

    assert db.added == []


async def test_refusal_detail_names_email_and_provider_for_the_operator():
    """The exception message is the log line the operator acts on (FR-008
    style: detail server-side only), so it must carry both identifiers."""
    with pytest.raises(SSOUserNotProvisionedError) as excinfo:
        await _get_or_create_sso_user(
            _FakeDb(),
            "nobody@example.com",
            "No Body",
            "saml",
            "subject-1",
            auto_create=False,
        )

    assert "nobody@example.com" in str(excinfo.value)
    assert "saml" in str(excinfo.value)


async def test_existing_account_matched_by_sso_subject_still_signs_in():
    """Disabling on-the-fly creation must not lock out provisioned users."""
    existing = User(
        email="alice@example.com",
        full_name="Alice",
        role="user",
        sso_provider="oidc",
        sso_subject="subject-1",
        is_active=True,
    )
    db = _FakeDb(by_subject=existing)

    user = await _get_or_create_sso_user(
        db,
        "alice@example.com",
        "Alice",
        "oidc",
        "subject-1",
        auto_create=False,
    )

    assert user is existing
    assert db.added == []


async def test_existing_account_is_still_linked_by_email():
    """Email linking is the pre-provisioning path itself — an admin creates
    the account, the first SSO login binds the subject to it. It must keep
    working with the toggle off, or the toggle would defeat its own purpose.
    """
    provisioned = User(
        email="bob@example.com", full_name="Bob", role="user", is_active=True
    )
    db = _FakeDb(by_email=provisioned)

    user = await _get_or_create_sso_user(
        db,
        "bob@example.com",
        "Bob",
        "saml",
        "subject-2",
        auto_create=False,
    )

    assert user is provisioned
    assert user.sso_provider == "saml"
    assert user.sso_subject == "subject-2"
    assert db.added == []


async def test_default_keeps_historical_auto_provisioning():
    """``auto_create`` defaults to True so existing deployments (and callers
    that never pass it) keep the historical create-on-first-login behavior."""
    db = _FakeDb()

    user = await _get_or_create_sso_user(
        db,
        "carol@example.com",
        "Carol",
        "oidc",
        "subject-3",
    )

    assert db.added == [user]
    assert user.email == "carol@example.com"


def test_not_provisioned_redirect_carries_only_the_opaque_marker(monkeypatch):
    """The person's identity stays out of the URL — the redirect names the
    outcome, never the email or provider detail (that goes to the log)."""
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")

    response = _sso_error_redirect("not_provisioned")

    assert response.headers["location"] == (
        "https://app.example.com/login?sso_error=not_provisioned"
    )


def test_saml_acs_refusal_uses_303_so_the_browser_follows_with_get(monkeypatch):
    """The ACS is a POST endpoint: a 307 would re-POST the SAML form to the
    login page. The refusal must use 303 (like the ACS success redirect)."""
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")

    assert _sso_error_redirect("not_provisioned", status_code=303).status_code == 303
    # The OIDC callback is a GET, where the default (307) is fine.
    assert _sso_error_redirect("not_provisioned").status_code == 307


class _RecordingDb(_FakeDb):
    """``_FakeDb`` that keeps the bound email of each by-email lookup."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.email_params: list = []

    async def execute(self, stmt):
        if "lower(users.email) =" in str(stmt):
            self.email_params.extend(stmt.compile().params.values())
        return await super().execute(stmt)


async def test_idp_email_case_does_not_matter_for_linking_or_creation():
    """An IdP returns ``First.Last@…``; accounts are stored lowercased.

    The lookup is by ``lower(email)`` against the lowercased address, and a
    new account is stored lowercased, so the same person never gets two rows.
    """
    existing = User(
        email="first.last@example.com", full_name="F L", role="user", is_active=True
    )
    db = _RecordingDb(by_email=existing)

    user = await _get_or_create_sso_user(
        db, "  First.Last@Example.com ", "F L", "oidc", "subject-1"
    )

    assert user is existing
    assert db.email_params == ["first.last@example.com"]

    created = await _get_or_create_sso_user(
        _RecordingDb(), "New.Person@Example.com", "N P", "saml", "subject-2"
    )

    assert created.email == "new.person@example.com"
