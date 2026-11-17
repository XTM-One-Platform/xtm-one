"""Regression tests for OAuth token-request shaping (``build_token_request``).

The Notion token endpoint requires HTTP Basic auth (base64 ``client_id:client_secret``
in the ``Authorization`` header) + a JSON body for BOTH the authorization-code
exchange and the refresh grant, and rejects credentials-in-body with
``401 invalid_client``. Atlassian/Jira needs a JSON body with the credentials
inline; every other provider uses a form-encoded body.

These tests pin the per-provider ``httpx.post()`` kwargs so the auth style can't
drift across the (now shared) code-exchange, inline-refresh, and scheduled-refresh
call sites.
"""

from __future__ import annotations

import base64

from app.integrations.oauth import (
    build_token_request,
    provider_uses_basic_auth_token,
)


def _expected_basic(client_id: str, client_secret: str) -> str:
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")


def test_provider_uses_basic_auth_token():
    assert provider_uses_basic_auth_token("notion") is True
    assert provider_uses_basic_auth_token("jira") is False
    assert provider_uses_basic_auth_token("microsoft") is False
    assert provider_uses_basic_auth_token("") is False


def test_notion_refresh_uses_basic_auth_and_json_body():
    params = {"grant_type": "refresh_token", "refresh_token": "r-tok"}
    kwargs = build_token_request("notion", "cid", "csecret", params)

    # JSON body, never form-encoded.
    assert "data" not in kwargs
    assert kwargs["json"] == {"grant_type": "refresh_token", "refresh_token": "r-tok"}
    # Credentials live in the Authorization header, NOT the body.
    assert "client_id" not in kwargs["json"]
    assert "client_secret" not in kwargs["json"]
    assert kwargs["headers"]["Authorization"] == f"Basic {_expected_basic('cid', 'csecret')}"
    assert kwargs["headers"]["Accept"] == "application/json"


def test_notion_code_exchange_uses_basic_auth_and_json_body():
    params = {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://app/callback",
    }
    kwargs = build_token_request("notion", "cid", "csecret", params)

    assert kwargs["json"] == params
    assert "client_id" not in kwargs["json"]
    assert "client_secret" not in kwargs["json"]
    assert "data" not in kwargs
    assert kwargs["headers"]["Authorization"] == f"Basic {_expected_basic('cid', 'csecret')}"


def test_jira_refresh_uses_json_body_with_inline_credentials():
    params = {"grant_type": "refresh_token", "refresh_token": "r-tok"}
    kwargs = build_token_request("jira", "cid", "csecret", params, json_provider=True)

    assert "data" not in kwargs
    assert kwargs["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r-tok",
        "client_id": "cid",
        "client_secret": "csecret",
    }
    # No Basic auth for Jira — credentials are in the JSON body.
    assert "Authorization" not in kwargs["headers"]


def test_default_provider_uses_form_body_with_inline_credentials():
    params = {
        "grant_type": "refresh_token",
        "refresh_token": "r-tok",
        "scope": "read write",
    }
    kwargs = build_token_request("microsoft", "cid", "csecret", params)

    assert "json" not in kwargs
    assert kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r-tok",
        "scope": "read write",
        "client_id": "cid",
        "client_secret": "csecret",
    }
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["headers"]["Accept"] == "application/json"


def test_a_public_client_sends_no_client_secret():
    params = {"grant_type": "refresh_token", "refresh_token": "rt"}
    kwargs = build_token_request("openapi", "public-app", "", params)
    assert kwargs["data"] == {"grant_type": "refresh_token", "refresh_token": "rt", "client_id": "public-app"}


def test_does_not_mutate_caller_params():
    """The helper copies ``params`` — adding credentials must not leak back into
    the caller's dict (which is reused/logged elsewhere)."""
    params = {"grant_type": "refresh_token", "refresh_token": "r-tok"}
    build_token_request("microsoft", "cid", "csecret", params)
    assert params == {"grant_type": "refresh_token", "refresh_token": "r-tok"}


# ---------------------------------------------------------------------------
# apply_oauth_token_expiry — missing expires_in must stamp a synthetic TTL
# ---------------------------------------------------------------------------


def test_apply_oauth_token_expiry_uses_provider_lifetime():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.integrations.oauth import apply_oauth_token_expiry

    target = SimpleNamespace(token_expires_at=None)
    before = datetime.now(UTC)
    apply_oauth_token_expiry(target, {"expires_in": 7200})
    after = datetime.now(UTC)
    assert target.token_expires_at is not None
    assert timedelta(seconds=7190) <= (target.token_expires_at - before) <= timedelta(
        seconds=7200
    ) + (after - before)


def test_apply_oauth_token_expiry_stamps_synthetic_ttl_when_omitted():
    """Notion without token rotation omits expires_in.  Leaving the column
    NULL put the row on the every-60s refresh sweep."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.integrations.oauth import (
        UNKNOWN_EXPIRY_TTL_SECONDS,
        apply_oauth_token_expiry,
    )

    target = SimpleNamespace(token_expires_at=None)
    before = datetime.now(UTC)
    apply_oauth_token_expiry(target, {"access_token": "nt", "refresh_token": "rt"})
    assert target.token_expires_at is not None
    delta = target.token_expires_at - before
    assert timedelta(seconds=UNKNOWN_EXPIRY_TTL_SECONDS - 5) <= delta <= timedelta(
        seconds=UNKNOWN_EXPIRY_TTL_SECONDS + 5
    )


def test_apply_oauth_token_expiry_rejects_unusable_lifetime():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.integrations.oauth import (
        UNKNOWN_EXPIRY_TTL_SECONDS,
        apply_oauth_token_expiry,
    )

    target = SimpleNamespace(token_expires_at=None, refresh_token_enc="enc-rt")
    before = datetime.now(UTC)
    apply_oauth_token_expiry(target, {"expires_in": 0})
    delta = target.token_expires_at - before
    assert delta >= timedelta(seconds=UNKNOWN_EXPIRY_TTL_SECONDS - 5)

    target.token_expires_at = None
    apply_oauth_token_expiry(target, {"expires_in": "not-a-number"})
    assert target.token_expires_at is not None


def test_apply_oauth_token_expiry_keeps_no_expiry_for_a_token_nothing_can_renew():
    """A token issued with no lifetime and no refresh token (a GitHub
    OAuth App's) never expires as far as the platform knows. A synthetic hour
    made it read as expired an hour later, and every run logged it."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.integrations.oauth import apply_oauth_token_expiry

    target = SimpleNamespace(token_expires_at=datetime.now(UTC) - timedelta(days=1), refresh_token_enc=None)
    apply_oauth_token_expiry(target, {"access_token": "gho_x", "token_type": "bearer"})
    assert target.token_expires_at is None

    # A stated lifetime still counts without a refresh token (LinkedIn's 60 days).
    apply_oauth_token_expiry(target, {"access_token": "x", "expires_in": 5184000})
    assert target.token_expires_at is not None
