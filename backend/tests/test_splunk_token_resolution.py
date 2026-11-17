"""Regression tests for Splunk compound-token resolution in
``integration_tools._get_token``.

Splunk's compound token is ``base_url||username||token``: the secret (a Splunk
authentication token for Bearer auth, or the password for HTTP Basic) is the
api_key; the management base URL and the optional username live in extra_config.
A blank username selects Bearer-token auth; a set username selects HTTP Basic.
Splunk is a required-``base_url`` provider, so a missing/blank base_url returns
``None`` (the misconfiguration surfaces) rather than an unusable token, and
surrounding whitespace on base_url/username is stripped.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_TOKEN = "splunk-auth-token-secret-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "splunk-prod"
        self.provider = "splunk"
        self.auth_type = "api_key"
        self.access_token_enc = None
        self.api_key_enc = b"blob"
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
    """Invoke ``_get_token`` for a Splunk integration whose ``extra_config``
    dict the test controls.
    """
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _TOKEN)

    async def _reveal(entity, column):
        return _TOKEN if getattr(entity, column) else None

    monkeypatch.setattr(integration_tools, "reveal_field", _reveal)

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
            provider="splunk",
        )

    return _runner


@pytest.mark.asyncio
async def test_bearer_token_no_username(_resolve):
    token = await _resolve({"base_url": "https://splunk.example.com:8089/"})
    # trailing slash stripped; blank username segment preserved for Bearer auth
    assert token == f"https://splunk.example.com:8089||||{_TOKEN}"
    base_url, username, secret = parse_compound_token(token, 3)
    assert base_url == "https://splunk.example.com:8089"
    assert username == ""
    assert secret == _TOKEN


@pytest.mark.asyncio
async def test_basic_auth_with_username(_resolve):
    token = await _resolve({"base_url": "https://splunk:8089", "username": "admin"})
    base_url, username, secret = parse_compound_token(token, 3)
    assert base_url == "https://splunk:8089"
    assert username == "admin"
    assert secret == _TOKEN


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_stripped(_resolve):
    token = await _resolve(
        {"base_url": "  https://splunk:8089  ", "username": "  admin  "}
    )
    base_url, username, secret = parse_compound_token(token, 3)
    assert base_url == "https://splunk:8089"
    assert username == "admin"
    assert secret == _TOKEN


@pytest.mark.asyncio
async def test_missing_or_blank_base_url_returns_none(_resolve):
    assert await _resolve({"username": "admin"}) is None
    assert await _resolve({"base_url": "   "}) is None


@pytest.mark.asyncio
async def test_no_extra_config_returns_none(_resolve):
    assert await _resolve(None) is None
