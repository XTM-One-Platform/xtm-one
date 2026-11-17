"""Regression tests for ServiceNow compound-token resolution in
``integration_tools._get_token``.

ServiceNow's compound token is ``base_url||username||password`` (the password is
the api_key secret; the instance URL + username live in extra_config). The
contract guarded here: ``_get_token`` returns the (possibly partial) compound
token rather than ``None`` so the tools build and the provider surfaces a clear
authentication error, matching the qualys / sumologic partial-token behavior.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_PASSWORD = "sn-secret-password-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None, api_key_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "servicenow-prod"
        self.provider = "servicenow"
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
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _PASSWORD)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _PASSWORD)

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
            provider="servicenow",
        )

    return _runner


@pytest.mark.asyncio
async def test_full_credentials_returns_compound(_resolve):
    token = await _resolve(
        {"base_url": "https://acme.service-now.com/", "username": "svc"}
    )
    # Trailing slash stripped; segments are base_url||username||password.
    assert token == f"https://acme.service-now.com||svc||{_PASSWORD}"
    base_url, username, password = parse_compound_token(token, 3)
    assert base_url == "https://acme.service-now.com"
    assert username == "svc"
    assert password == _PASSWORD


@pytest.mark.asyncio
async def test_missing_username_returns_partial_token_not_none(_resolve):
    token = await _resolve({"base_url": "https://acme.service-now.com"})
    assert token is not None
    base_url, username, password = parse_compound_token(token, 3)
    assert base_url == "https://acme.service-now.com"
    assert username == ""
    assert password == _PASSWORD


@pytest.mark.asyncio
async def test_missing_password_returns_partial_token_not_none(_resolve):
    token = await _resolve({"base_url": "https://acme.service-now.com", "username": "svc"}, has_api_key=False)
    assert token is not None
    base_url, username, password = parse_compound_token(token, 3)
    assert base_url == "https://acme.service-now.com"
    assert username == "svc"
    assert password == ""


@pytest.mark.asyncio
async def test_partial_token_still_builds_tools_with_runtime_guard(_resolve):
    """A credential-less token (but valid base URL) still exposes the tool set;
    the tools then surface ServiceNow's authentication error at call time.
    """
    from app.integrations.providers.servicenow import build_tools
    from app.tools.base import CallableToolExecutor

    token = await _resolve({"base_url": "https://acme.service-now.com"}, has_api_key=False)
    assert token is not None
    executor = CallableToolExecutor()
    tools = build_tools(token=token, executor=executor)
    assert tools, "a base-only ServiceNow token must still expose its tools"
