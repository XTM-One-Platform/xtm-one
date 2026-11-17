"""The Google consent asks for gmail.modify, and a sign-in stores it.

Google is a native provider: the authorize step and the OAuth callback reset
an integration's scopes to the catalog's. When the catalog asked only for
gmail.readonly and gmail.compose, no reconnect could grant gmail.modify, and
starring, labelling and trashing mail answered 403 on every Google
integration. No provider is reached: the code exchange is a double.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.api.oauth_callbacks as callbacks
import app.services.token_refresh as token_refresh
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.core.config import settings
from app.models.integration import Integration
from app.models.user import User
from tests.integration_credential_fakes import COLLEAGUE_ID, OWNER_ID, FakeSession, make_integration

GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def test_the_google_consent_asks_for_gmail_modify_and_keeps_every_scope():
    scopes = PROVIDER_CATALOG["google"]["scopes"].split()

    assert GMAIL_MODIFY in scopes
    for kept in (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive",
        "openid",
    ):
        assert kept in scopes
    assert len(scopes) == len(set(scopes))


def test_the_google_setup_guide_lists_gmail_modify_and_says_to_reauthorize():
    guide = PROVIDER_CATALOG["google"]["setup_guide"]

    assert "gmail.modify" in [p["scope"] for p in guide["permissions"]]
    assert "gmail.modify" in guide["notes"]


@pytest.mark.asyncio
async def test_a_google_sign_in_stores_the_catalog_scopes_with_gmail_modify(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example")
    monkeypatch.setattr(callbacks, "oauth_exchange_spec", AsyncMock(return_value=None))
    monkeypatch.setattr(
        callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "the-access", "refresh_token": "the-refresh"}),
    )
    monkeypatch.setattr(callbacks, "_installations_seen_on_connect", AsyncMock(return_value=None))
    monkeypatch.setattr(callbacks, "store_authorized_tokens", AsyncMock())
    monkeypatch.setattr(callbacks, "mark_authorized", AsyncMock())
    monkeypatch.setattr(callbacks, "upsert_user_credential", AsyncMock(side_effect=lambda _db, i, _uid: i))
    monkeypatch.setattr(callbacks, "log_audit", AsyncMock())
    monkeypatch.setattr(token_refresh, "clear_refresh_cooldown", AsyncMock())
    stale = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose openid"
    integration = make_integration(provider="google", scopes=stale)
    users = [
        User(id=uid, email=f"{uid}@example.com", full_name="u", role="user", oauth_share_dismissed_integration_ids=[])
        for uid in (OWNER_ID, COLLEAGUE_ID)
    ]
    state = {"integration_id": str(integration.id), "user_id": str(OWNER_ID), "provider": "google"}

    await callbacks._complete_integration_callback(
        integration, state, "the-code", FakeSession({Integration: [integration], User: users})
    )

    assert integration.scopes == PROVIDER_CATALOG["google"]["scopes"]
    assert GMAIL_MODIFY in integration.scopes.split()
