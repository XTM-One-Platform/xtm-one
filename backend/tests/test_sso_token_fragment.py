"""SEC-7: the SSO login redirect must deliver the JWT in the URL FRAGMENT.

``auth_sso._build_frontend_redirect`` is the single place the backend hands
the freshly minted session JWT back to the browser after an OIDC or SAML
login. It must use ``{frontend_url}/login#token=...`` — a fragment is
processed client-side only and is never sent to servers, written to
proxy/access logs, or leaked through ``Referer`` headers — and must NOT
regress to the old ``?token=...`` query-string form, which leaks through
all three.

The frontend login page reads the fragment first (with a legacy
query-string fallback for in-flight flows) and scrubs it from the address
bar after consumption.

The same builder may hand back a two-factor *challenge* token
(``#mfa_token=...``) instead of a session token, when the account is enrolled.
That is a bearer credential on the same terms, so the fragment rule covers it
too.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.api import auth_sso as auth_sso_module
from app.api.auth_sso import _build_frontend_redirect
from app.core.config import settings

FAKE_JWT = "header.payload.signature"


def _make_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


def _patch_token(monkeypatch) -> None:
    """Stub the JWT mint so the test pins the redirect *format* without
    depending on the signing configuration (``settings.secret_key``).
    """
    monkeypatch.setattr(
        auth_sso_module, "create_access_token", lambda _uid: FAKE_JWT
    )


def test_redirect_puts_token_in_fragment(monkeypatch):
    """The token must travel in the fragment: ``/login#token=...``."""
    _patch_token(monkeypatch)

    url = _build_frontend_redirect(_make_user(), provider="oidc")

    expected_base = (settings.frontend_url or settings.base_url).rstrip("/")
    assert url == f"{expected_base}/login#token={FAKE_JWT}"


def test_redirect_falls_back_to_base_url_and_strips_trailing_slash(monkeypatch):
    """The redirect builder must follow the repo-wide frontend-link
    convention: ``frontend_url`` for split-origin deployments, ``base_url``
    as the single-origin fallback, trailing slashes stripped so the result
    never contains ``//login`` (matches ``auth.forgot_password``,
    ``users.send_password_reset``, ``invitations``, ``oauth_callbacks``).
    """
    _patch_token(monkeypatch)

    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com/")
    url = _build_frontend_redirect(_make_user(), provider="oidc")
    assert url == f"https://app.example.com/login#token={FAKE_JWT}"

    monkeypatch.setattr(settings, "frontend_url", "")
    monkeypatch.setattr(settings, "base_url", "https://single.example.com/")
    url = _build_frontend_redirect(_make_user(), provider="oidc")
    assert url == f"https://single.example.com/login#token={FAKE_JWT}"


def test_redirect_never_puts_token_in_query_string(monkeypatch):
    """Regression guard for SEC-7: a query string (``?token=``) leaks the
    JWT via browser history, server/proxy logs, and Referer headers — the
    redirect must not contain one at all.
    """
    _patch_token(monkeypatch)

    url = _build_frontend_redirect(_make_user(), provider="oidc")

    assert "?token=" not in url
    assert "?" not in url, "the SSO redirect must carry no query string"
    assert "#token=" in url


def test_mfa_challenge_redirect_obeys_the_same_fragment_rule(monkeypatch):
    """SEC-7 extends to the two-factor challenge.

    When the account is enrolled, the redirect carries a challenge token
    instead of a session token. That challenge is a bearer credential too — it
    completes a login — so it must never travel in a query string either.
    """
    from app.services import mfa

    secret = mfa.generate_secret()
    user = SimpleNamespace(
        id=uuid.uuid4(), mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret)
    )

    url = _build_frontend_redirect(user, provider="saml")

    assert "#mfa_token=" in url
    assert "?" not in url, "the SSO redirect must carry no query string"
    # And it is a challenge, not a session — the two must not be confusable.
    assert "#token=" not in url
