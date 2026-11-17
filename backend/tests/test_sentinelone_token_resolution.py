"""Regression tests for SentinelOne compound-token resolution in
``integration_tools._get_token``.

SentinelOne (like Metabase / OpenCTI / OpenAEV / n8n / Grafana / YouTrack)
is configured as the compound ``base_url||api_token`` that the provider
splits with ``parse_compound_token(token, 2)`` and validates *base URL
first*.

The bug these tests guard against: when the console base URL was missing
(no ``extra_config`` row, or an empty ``base_url``), ``_get_token`` returned
the **bare** API token with no ``||`` delimiter.  ``parse_compound_token``
then mapped the whole token onto the first field (``base_url``), leaving the
api_token empty — so the provider reported a misleading "API token is not
configured" error even though the token was set and the *base URL* was the
thing actually missing.

The fix preserves the ``||`` delimiter (returning ``||token``) so the empty
segment lands on ``base_url`` and the provider surfaces the accurate
"console base URL is not configured" error instead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_API_TOKEN = "s1-management-token-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "sentinelone-prod"
        self.provider = "sentinelone"
        self.auth_type = "api_key"
        self.access_token_enc = None
        self.api_key_enc = b"encrypted-api-key"
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
    """Invoke ``_get_token`` for a SentinelOne integration whose
    ``extra_config`` dict the test controls.
    """
    monkeypatch.setattr(
        integration_tools, "decrypt_value", lambda _blob: _API_TOKEN
    )
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _API_TOKEN)

    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(extra: dict[str, Any] | None) -> str | None:
        if extra is None:
            integ = _FakeIntegration(extra_config_enc=None)
        else:
            monkeypatch.setattr(
                integration_tools, "decrypt_json", lambda _blob, _e=extra: dict(_e)
            )
            # extra_config is read through the vault resolver (#6080), which decrypts in app.core.security.
            monkeypatch.setattr("app.core.security.decrypt_json", integration_tools.decrypt_json)
            integ = _FakeIntegration(extra_config_enc=b"blob")
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider="sentinelone",
        )

    return _runner


@pytest.mark.asyncio
async def test_base_url_present_returns_compound(_resolve):
    token = await _resolve({"base_url": "https://mgmt.sentinelone.net/"})
    # Trailing slash is stripped; base_url is the first segment.
    assert token == f"https://mgmt.sentinelone.net||{_API_TOKEN}"
    base_url, api_token = parse_compound_token(token, 2)
    assert base_url == "https://mgmt.sentinelone.net"
    assert api_token == _API_TOKEN


@pytest.mark.asyncio
async def test_missing_extra_config_preserves_delimiter(_resolve):
    token = await _resolve(None)
    assert token == f"||{_API_TOKEN}"
    base_url, api_token = parse_compound_token(token, 2)
    # The empty segment must land on base_url, NOT swallow the api_token.
    assert base_url == ""
    assert api_token == _API_TOKEN


@pytest.mark.asyncio
async def test_empty_base_url_preserves_delimiter(_resolve):
    token = await _resolve({"base_url": ""})
    assert token == f"||{_API_TOKEN}"
    base_url, api_token = parse_compound_token(token, 2)
    assert base_url == ""
    assert api_token == _API_TOKEN


@pytest.mark.asyncio
async def test_missing_base_url_surfaces_base_url_error_not_token_error(_resolve):
    """End-to-end: a missing base URL must produce the accurate
    "base URL not configured" error, not a misleading token error.
    """
    from app.integrations.providers.sentinelone import build_tools
    from app.tools.base import CallableToolExecutor, ToolCall

    token = await _resolve(None)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="1", name="list_sentinelone_threats", arguments={})
    )
    text = result.content.lower()
    assert "base url" in text or "console" in text
    assert "api token is not configured" not in text
