"""Regression tests for Sumo Logic compound-token resolution in
``integration_tools._get_token``.

Sumo Logic's compound token is ``base_url||access_id||access_key`` (the Access
Key is the api_key secret; the Access ID + region base URL live in
extra_config, with base_url defaulting to the US1 endpoint). The key contract
guarded here: when the Access ID or Access Key is missing, ``_get_token`` must
still return the (partial) compound token rather than ``None``. Returning
``None`` makes ``build_integration_tools`` skip the provider entirely
(``if not token: continue``), which hides the misconfiguration and prevents the
provider's call-time guard from surfacing the clear "credentials are not
configured" error - matching the infoblox/rapid7 and metabase/opencti
partial-token behavior.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_ACCESS_KEY = "sumo-access-key-secret-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None, api_key_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "sumologic-prod"
        self.provider = "sumologic"
        self.auth_type = "api_key"
        self.access_token_enc = None
        self.api_key_enc = api_key_enc
        self.extra_config_enc = extra_config_enc
        self.refresh_token_enc = None
        self.token_expires_at = None
        self.token_meta = None
        self.installation_id = None
        self.is_company_managed = False
        self.user_id = None
        self.scope_kind = None
        self.scope_id = None
        self.binding_metadata = None


class _FakeDB:
    def __init__(self, integ):
        self._integ = integ

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._integ)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def _resolve(monkeypatch):
    """Invoke ``_get_token`` for a Sumo Logic integration whose ``extra_config``
    dict and api_key presence the test controls.
    """
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _ACCESS_KEY)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _ACCESS_KEY)

    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(
        extra: dict[str, Any] | None, *, has_api_key: bool = True
    ) -> str | None:
        api_key_enc = b"blob" if has_api_key else None
        if extra is None:
            integ = _FakeIntegration(extra_config_enc=None, api_key_enc=api_key_enc)
        else:
            monkeypatch.setattr(
                integration_tools, "decrypt_json", lambda _blob, _e=extra: dict(_e)
            )
            # extra_config is read through the vault resolver (#6080), which decrypts in app.core.security.
            monkeypatch.setattr("app.core.security.decrypt_json", integration_tools.decrypt_json)
            integ = _FakeIntegration(extra_config_enc=b"blob", api_key_enc=api_key_enc)
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider="sumologic",
        )

    return _runner


@pytest.mark.asyncio
async def test_full_credentials_returns_compound(_resolve):
    token = await _resolve(
        {"base_url": "https://api.eu.sumologic.com/api/", "access_id": "aid-1"}
    )
    # Trailing slash stripped; segments are base_url||access_id||access_key.
    assert token == f"https://api.eu.sumologic.com/api||aid-1||{_ACCESS_KEY}"
    base_url, access_id, access_key = parse_compound_token(token, 3)
    assert base_url == "https://api.eu.sumologic.com/api"
    assert access_id == "aid-1"
    assert access_key == _ACCESS_KEY


@pytest.mark.asyncio
async def test_missing_access_id_returns_partial_token_not_none(_resolve):
    # Missing Access ID must NOT collapse to None (which would skip the provider
    # and hide the misconfiguration); the compound delimiter is preserved.
    token = await _resolve({"base_url": "https://api.sumologic.com/api"})
    assert token is not None
    base_url, access_id, access_key = parse_compound_token(token, 3)
    assert base_url == "https://api.sumologic.com/api"
    assert access_id == ""
    assert access_key == _ACCESS_KEY


@pytest.mark.asyncio
async def test_missing_access_key_returns_partial_token_not_none(_resolve):
    # Missing Access Key (no api_key_enc) must also return a token, defaulting
    # the base_url to the US1 endpoint.
    token = await _resolve({"access_id": "aid-1"}, has_api_key=False)
    assert token is not None
    base_url, access_id, access_key = parse_compound_token(token, 3)
    assert base_url == "https://api.sumologic.com/api"
    assert access_id == "aid-1"
    assert access_key == ""


@pytest.mark.asyncio
async def test_partial_token_still_builds_tools_with_runtime_guard(_resolve):
    """End-to-end: a credential-less token still exposes the tool set, and the
    tools fail with the provider's clear call-time credential error.
    """
    from app.integrations.providers.sumologic import build_tools
    from app.tools.base import CallableToolExecutor, ToolCall

    token = await _resolve(None, has_api_key=False)
    assert token is not None
    executor = CallableToolExecutor()
    tools = build_tools(token=token, executor=executor)
    assert tools, "a credential-less Sumo Logic token must still expose its tools"

    result = await executor.execute_tool(
        ToolCall(id="t0", name="get_sumologic_status", arguments={})
    )
    assert "credentials are not configured" in result.content.lower()
