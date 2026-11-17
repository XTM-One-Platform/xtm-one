"""Regression tests for Cortex XSOAR/XDR compound-token resolution in
``integration_tools._get_token``.

Both providers assemble ``base_url||api_key||api_key_id||auth_mode`` from
``extra_config``. Cortex XDR **requires** the API Key ID (``x-xdr-auth-id``):
without it ``xdr.build_tools`` early-returns zero tools, so a misconfigured XDR
integration would silently produce no tools. ``_get_token`` guards this case by
returning ``None`` (and logging) — consistent with ``integration_testing.py``,
which explicitly errors on a missing ``api_key_id`` for XDR.

Cortex XSOAR, by contrast, legitimately leaves ``api_key_id`` empty for the
v6/on-prem auth mode, so the guard must be XDR-only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools

_API_KEY = "cortex-api-key-xyz"


class _FakeIntegration:
    def __init__(self, *, provider: str, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = f"{provider}-prod"
        self.provider = provider
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
    """Invoke ``_get_token`` for a Cortex integration whose ``extra_config``
    dict the test controls.
    """
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _API_KEY)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _API_KEY)

    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(provider: str, extra: dict[str, Any] | None) -> str | None:
        if extra is None:
            integ = _FakeIntegration(provider=provider, extra_config_enc=None)
        else:
            monkeypatch.setattr(
                integration_tools, "decrypt_json", lambda _blob, _e=extra: dict(_e)
            )
            # extra_config is read through the vault resolver (#6080), which decrypts in app.core.security.
            monkeypatch.setattr("app.core.security.decrypt_json", integration_tools.decrypt_json)
            integ = _FakeIntegration(provider=provider, extra_config_enc=b"blob")
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider=provider,
        )

    return _runner


@pytest.mark.asyncio
async def test_xdr_full_compound(_resolve):
    token = await _resolve(
        "xdr",
        {
            "base_url": "https://api.xdr.example/",
            "api_key_id": "42",
            "auth_mode": "advanced",
        },
    )
    assert token == f"https://api.xdr.example||{_API_KEY}||42||advanced"


@pytest.mark.asyncio
async def test_xdr_missing_api_key_id_returns_none(_resolve):
    # base_url is present, but XDR mandates api_key_id — _get_token must surface
    # the misconfiguration as None rather than a compound token that yields zero
    # tools downstream.
    token = await _resolve(
        "xdr", {"base_url": "https://api.xdr.example", "api_key_id": ""}
    )
    assert token is None


@pytest.mark.asyncio
async def test_xsoar_empty_api_key_id_still_compound(_resolve):
    # XSOAR v6/on-prem legitimately has no api_key_id; the XDR guard must not
    # apply, so a compound token (with an empty api_key_id segment) is returned.
    token = await _resolve(
        "xsoar", {"base_url": "https://xsoar.example", "api_key_id": ""}
    )
    assert token == f"https://xsoar.example||{_API_KEY}||||standard"
