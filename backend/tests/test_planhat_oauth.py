"""Planhat connects with each person's own account (Planhat OAuth Clients).

The catalog offers OAuth next to the Private App API key: the authorization
goes to Planhat's own sign-in (and so its SSO), asks the ``inclusive`` scope
(the client's permissions intersected with the person's Planhat role) and the
tools send the person's access token as the bearer, as they send an API key.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, supported_auth_types
from app.core.security import encrypt_value
from app.integrations import oauth as oauth_module
from app.integrations.providers.planhat import build_tools

_PLANHAT = PROVIDER_CATALOG["planhat"]


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        self.store[key] = value
        return True


def test_planhat_offers_oauth_and_the_api_key():
    assert supported_auth_types(_PLANHAT) == ["oauth", "api_key"]
    assert _PLANHAT["auth_type"] == "oauth"
    # Both modes' credentials: the form offers the API key or the OAuth client.
    assert set(_PLANHAT["fields"]) == {"client_id", "client_secret", "api_key"}
    assert _PLANHAT["authorize_url"] == "https://ws.planhat.com/oauth/authorize"
    assert _PLANHAT["token_url"] == "https://api.planhat.com/oauth/token"
    # The person's role caps what the client grants, so a narrower role
    # still authorizes instead of being refused.
    assert _PLANHAT["scopes"] == "inclusive"


@pytest.mark.asyncio
async def test_planhat_authorize_url_asks_the_inclusive_scope(monkeypatch):
    fake = _FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(oauth_module, "redis_client", _redis)
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider="planhat",
        auth_type="oauth",
        client_id_enc=encrypt_value("pk_test"),
        client_secret_enc=encrypt_value("sk_test"),
        scopes=_PLANHAT["scopes"],
        authorize_url=_PLANHAT["authorize_url"],
        token_url=_PLANHAT["token_url"],
        extra_config_enc=None,
    )

    url = await oauth_module.build_authorize_url_for_integration(integration, str(uuid.uuid4()))

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://ws.planhat.com/oauth/authorize"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert query["client_id"] == "pk_test"
    assert query["response_type"] == "code"
    assert query["scope"] == "inclusive"
    assert query["redirect_uri"].endswith("/api/oauth/callback")
    state = json.loads(fake.store[f"oauth_state:{query['state']}"])
    assert state["integration_id"] == str(integration.id)
    assert state["provider"] == "planhat"


@pytest.mark.asyncio
async def test_planhat_tools_send_the_oauth_access_token_as_bearer():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("oauth-access-token", executor)
    search = next(
        call[0][1]
        for call in executor.register.call_args_list
        if call[0][0] == "search_planhat_companies"
    )

    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=[]), None)
        await search(query="Acme")

    headers = mock_req.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer oauth-access-token"


class _EmptyResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _EmptyDb:
    async def execute(self, _stmt):
        return _EmptyResult()


@pytest.mark.asyncio
async def test_the_catalog_serves_one_guide_per_mode_with_placeholders_filled(monkeypatch):
    from app.api.v1 import integrations as integrations_api

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=[])

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    monkeypatch.setattr("app.services.branding.get_branding", _branding)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")

    items = await integrations_api.list_provider_catalog(
        category="integration", include_disabled=False, user=user, db=_EmptyDb()
    )

    planhat = next(item for item in items if item.provider == "planhat")
    assert planhat.setup_guide is not None
    guides = planhat.auth_setup_guides or {}
    assert set(guides) == {"oauth", "api_key"}
    oauth_steps = " ".join(guides["oauth"].steps)
    assert "App Center > New App > OAuth Client" in oauth_steps
    assert "/api/oauth/callback" in oauth_steps
    assert "{base_url}" not in oauth_steps and "{APP_NAME}" not in oauth_steps
    assert "'XTM One'" in oauth_steps
    assert "Private App" in " ".join(guides["api_key"].steps)
    # A provider without per-mode guides serves none.
    jira = next(item for item in items if item.provider == "jira")
    assert jira.auth_setup_guides is None
