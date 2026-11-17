"""Regression tests for Rapid7 compound-token resolution in
``integration_tools._get_token``.

Unlike the required-base_url providers (Metabase / OpenCTI / OpenAEV / n8n /
Grafana / YouTrack / Tanium / SentinelOne), Rapid7 - like Infoblox - treats
``base_url`` as OPTIONAL: the provider defaults an empty base URL to the US
region host (``https://us.api.insight.rapid7.com``). ``_get_token`` therefore
returns ``base_url||token`` (or ``||token`` for the default) and must NOT emit
the misleading "set the base URL" warning that the required-base_url providers
log - a region-less Rapid7 integration is a valid configuration, not a
misconfiguration.

These tests guard that contract: the compound delimiter is always preserved,
no base_url-missing warning is logged for Rapid7, and the missing-base_url
token still produces a working tool set that targets the default US host.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_API_KEY = "rapid7-insight-api-key-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "rapid7-prod"
        self.provider = "rapid7"
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
    """Invoke ``_get_token`` for a Rapid7 integration whose ``extra_config``
    dict the test controls.
    """
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _API_KEY)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _API_KEY)

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
            provider="rapid7",
        )

    return _runner


@pytest.mark.asyncio
async def test_base_url_present_returns_compound(_resolve):
    token = await _resolve({"base_url": "https://eu.api.insight.rapid7.com/"})
    # Trailing slash is stripped; base_url is the first segment.
    assert token == f"https://eu.api.insight.rapid7.com||{_API_KEY}"
    base_url, api_key = parse_compound_token(token, 2)
    assert base_url == "https://eu.api.insight.rapid7.com"
    assert api_key == _API_KEY


@pytest.mark.asyncio
async def test_base_url_whitespace_is_normalized(_resolve):
    # A pasted base_url with surrounding whitespace is stripped (and the
    # trailing slash trimmed) before being persisted into the compound token.
    token = await _resolve({"base_url": "  https://eu.api.insight.rapid7.com/  "})
    assert token == f"https://eu.api.insight.rapid7.com||{_API_KEY}"
    base_url, _ = parse_compound_token(token, 2)
    assert base_url == "https://eu.api.insight.rapid7.com"


@pytest.mark.asyncio
async def test_missing_extra_config_preserves_delimiter_without_warning(
    _resolve, caplog
):
    with caplog.at_level(logging.WARNING, logger="app.services.integration_tools"):
        token = await _resolve(None)
    assert token == f"||{_API_KEY}"
    base_url, api_key = parse_compound_token(token, 2)
    # Empty segment lands on base_url; the API key is preserved.
    assert base_url == ""
    assert api_key == _API_KEY
    # A region-less Rapid7 integration is valid - no "set the base URL" warning.
    assert not any("base url" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_base_url_preserves_delimiter_without_warning(_resolve, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.integration_tools"):
        token = await _resolve({"base_url": ""})
    assert token == f"||{_API_KEY}"
    base_url, api_key = parse_compound_token(token, 2)
    assert base_url == ""
    assert api_key == _API_KEY
    assert not any("base url" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_missing_base_url_still_exposes_tools_defaulting_to_us(_resolve):
    """End-to-end: the ``||token`` from a region-less integration still yields a
    working tool set that targets the default US Insight host.
    """
    from app.integrations.providers.rapid7 import build_tools
    from app.tools.base import CallableToolExecutor

    token = await _resolve(None)
    executor = CallableToolExecutor()
    tools = build_tools(token=token, executor=executor)
    assert tools, "a region-less Rapid7 token must still expose its tools"
