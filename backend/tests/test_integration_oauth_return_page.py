"""An integration's OAuth sign-in returns to that integration's page.

The callback used to send the browser to the integrations list, so a person
who connected their own account on a shared integration had to click
back in to see it connected. The return target is this platform's page of the
row the callback loaded: built from its id, never from text in the state or
from the provider, with an encoded query, so it cannot become an open redirect.
No provider is reached: the code exchange is a double.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

import app.api.oauth_callbacks as callbacks
import app.services.token_refresh as token_refresh
from app.core.config import settings
from app.models.integration import Integration
from app.models.user import User
from tests.integration_credential_fakes import COLLEAGUE_ID, OWNER_ID, FakeSession, make_integration

_FRONTEND = "https://xtm.example"


@pytest.fixture
def _exchange(monkeypatch):
    """The code exchange and what follows it, with no provider reached."""
    exchange = AsyncMock(return_value={"access_token": "the-access", "refresh_token": "the-refresh"})
    monkeypatch.setattr(settings, "frontend_url", _FRONTEND)
    monkeypatch.setattr(callbacks, "oauth_exchange_spec", AsyncMock(return_value=None))
    monkeypatch.setattr(callbacks, "exchange_code_for_tokens_with_integration", exchange)
    monkeypatch.setattr(callbacks, "_installations_seen_on_connect", AsyncMock(return_value=None))
    monkeypatch.setattr(callbacks, "store_authorized_tokens", AsyncMock())
    monkeypatch.setattr(callbacks, "mark_authorized", AsyncMock())
    monkeypatch.setattr(callbacks, "upsert_user_credential", AsyncMock(side_effect=lambda _db, integration, _uid: integration))
    monkeypatch.setattr(callbacks, "log_audit", AsyncMock())
    monkeypatch.setattr(token_refresh, "clear_refresh_cooldown", AsyncMock())
    return exchange


def _state(integration: Integration, **overrides) -> dict:
    state = {"integration_id": str(integration.id), "user_id": str(OWNER_ID), "provider": "slack"}
    state.update(overrides)
    return state


def _session(integration: Integration) -> FakeSession:
    users = [
        User(id=uid, email=f"{uid}@example.com", full_name="u", role="user", oauth_share_dismissed_integration_ids=[])
        for uid in (OWNER_ID, COLLEAGUE_ID)
    ]
    return FakeSession({Integration: [integration], User: users})


async def _location(integration: Integration, state: dict) -> str:
    response = await callbacks._complete_integration_callback(integration, state, "the-code", _session(integration))
    assert response.status_code == 307
    return response.headers["location"]


@pytest.mark.asyncio
async def test_a_completed_sign_in_returns_to_the_integration_page(_exchange):
    integration = make_integration()

    location = await _location(integration, _state(integration))

    assert location == f"{_FRONTEND}/integrations/{integration.id}?connected=slack"


@pytest.mark.asyncio
async def test_an_own_account_sign_in_returns_to_the_integration_page(_exchange):
    integration = make_integration(credential_mode="per_user", shared_user_ids=[str(COLLEAGUE_ID)])

    location = await _location(integration, _state(integration, user_id=str(COLLEAGUE_ID), credential="user"))

    assert location == f"{_FRONTEND}/integrations/{integration.id}?connected=slack"
    callbacks.upsert_user_credential.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refused_code_returns_to_the_integration_page_with_the_error(_exchange):
    _exchange.side_effect = RuntimeError("invalid_grant")
    integration = make_integration()

    location = await _location(integration, _state(integration))

    assert location == f"{_FRONTEND}/integrations/{integration.id}?error=token_exchange_failed&provider=slack"


@pytest.mark.parametrize(
    "provider",
    [
        "x&next=https://evil.example",
        "//evil.example",
        "https://evil.example/#frag",
        "a b\r\nLocation: https://evil.example",
        "../../admin?x=1",
        "",
    ],
)
@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.asyncio
async def test_no_state_text_leaves_the_integration_page(_exchange, provider, refused):
    if refused:
        _exchange.side_effect = RuntimeError("invalid_grant")
    integration = make_integration()
    # The id in the state names a different row than the one loaded: the
    # target is the loaded row's page, whatever the state says.
    state = _state(integration, provider=provider, integration_id=str(uuid.uuid4()))

    target = urlsplit(await _location(integration, state))

    assert (target.scheme, target.netloc, target.path, target.fragment) == (
        "https",
        "xtm.example",
        f"/integrations/{integration.id}",
        "",
    )
    expected = {"error": ["token_exchange_failed"], "provider": [provider]} if refused else {"connected": [provider]}
    assert parse_qs(target.query, keep_blank_values=True) == expected


@pytest.mark.parametrize(
    ("frontend_url", "base_url", "origin"),
    [
        ("https://xtm.example/", "https://unused.example", "https://xtm.example"),
        ("https://xtm.example/xtm-one", "https://unused.example", "https://xtm.example/xtm-one"),
        ("", "https://base.example/xtm-one/", "https://base.example/xtm-one"),
    ],
)
@pytest.mark.asyncio
async def test_the_page_is_on_the_configured_frontend(_exchange, monkeypatch, frontend_url, base_url, origin):
    monkeypatch.setattr(settings, "frontend_url", frontend_url)
    monkeypatch.setattr(settings, "base_url", base_url)
    integration = make_integration()

    location = await _location(integration, _state(integration))

    assert location == f"{origin}/integrations/{integration.id}?connected=slack"
