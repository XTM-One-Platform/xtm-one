"""Regression tests for the SSO ``Automatically admin users`` toggle.

These pin the contract of ``auth_sso._apply_auto_admin`` — the helper that
makes the toggle apply to *every* login through an SSO method, not just the
account-creation path.  The behaviour table the helper must keep
honouring:

| Scenario                                          | Toggle | Expected role |
|---------------------------------------------------|--------|---------------|
| Existing ``user`` (local / provisioned / old SSO) | ON     | ``admin`` ✅  |
| Existing ``admin``                                | OFF    | ``admin``     |
| Existing ``user``                                 | OFF    | ``user``      |
| Existing ``admin``                                | ON     | ``admin``     |

The helper is **promote-only**: it must never demote.  That is the security
property that keeps manually-assigned admins and the platform admin from being
stripped of their role when the toggle is later disabled — a regression that
turned this into a two-way sync would silently lock the platform admin out of
admin features on their next SSO login.
"""

from __future__ import annotations

import logging

from app.api.auth_sso import _apply_auto_admin
from app.models.user import User


def _user(role: str) -> User:
    """Build an in-memory ``User`` with an explicit role.

    No DB session is involved — ``_apply_auto_admin`` only reads/writes
    ``user.role`` and ``user.email``, so a detached instance is enough and
    keeps the test in the fast (``not integration``) suite.
    """
    return User(email="alice@example.com", full_name="Alice", role=role)


def test_existing_non_admin_is_promoted_when_toggle_on():
    """The bug this PR fixes: an account that already exists with role
    ``user`` (local registration, admin pre-provisioning, or an earlier
    non-admin SSO login) MUST be promoted to ``admin`` on its next SSO
    login while the toggle is on.  Before the fix only brand-new accounts
    were promoted, so these users stayed non-admin forever.
    """
    user = _user("user")
    _apply_auto_admin(user, auto_admin=True, sso_provider="oidc")
    assert user.role == "admin"


def test_existing_admin_is_not_demoted_when_toggle_off():
    """Promote-only contract: with the toggle OFF the helper must be a
    no-op, so a manually-assigned admin (or the platform admin) keeps
    their role.  A regression to a two-way sync would demote them here.
    """
    user = _user("admin")
    _apply_auto_admin(user, auto_admin=False, sso_provider="saml")
    assert user.role == "admin"


def test_existing_non_admin_is_unchanged_when_toggle_off():
    """With the toggle OFF a regular user must stay a regular user — the
    helper must not grant admin off the back of a plain SSO login.
    """
    user = _user("user")
    _apply_auto_admin(user, auto_admin=False, sso_provider="oidc")
    assert user.role == "user"


def test_existing_admin_is_unchanged_when_toggle_on():
    """Idempotency guard: an account that is already ``admin`` must remain
    ``admin`` when the toggle is on (and must not emit a spurious
    "promoted" log line — see the logging test below).
    """
    user = _user("admin")
    _apply_auto_admin(user, auto_admin=True, sso_provider="saml")
    assert user.role == "admin"


def test_promotion_is_logged(caplog):
    """A privilege escalation to admin is security-sensitive, so the helper
    must leave an INFO breadcrumb identifying the user and provider.  This
    pins that the audit trail (``logger.info``) is emitted on a real
    promotion so operators can trace how an account became admin.
    """
    user = _user("user")
    with caplog.at_level(logging.INFO, logger="app.api.auth_sso"):
        _apply_auto_admin(user, auto_admin=True, sso_provider="oidc")
    # Assert on the fully-rendered message (``getMessage()`` interpolates the
    # ``%s`` args) so the test reflects what operators actually see in the log.
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "admin" in msg.lower() and "oidc" in msg.lower() and user.email in msg
        for msg in messages
    ), (
        "a promotion must be logged with the user identifier (email) and "
        "provider for traceability"
    )


def test_noop_does_not_log(caplog):
    """The symmetry guard for the logging test: when nothing changes
    (toggle off, or the user is already admin) the helper must NOT log a
    promotion, otherwise the audit trail fills with misleading entries on
    every routine SSO login.
    """
    already_admin = _user("admin")
    plain_user_toggle_off = _user("user")
    with caplog.at_level(logging.INFO, logger="app.api.auth_sso"):
        _apply_auto_admin(already_admin, auto_admin=True, sso_provider="oidc")
        _apply_auto_admin(plain_user_toggle_off, auto_admin=False, sso_provider="saml")
    assert not caplog.records, (
        "no promotion log line should be emitted when the role does not change"
    )
