"""Regression tests for LogRhythm compound-token resolution in
``integration_tools._get_token``.

LogRhythm's compound token is ``base_url||api_token`` (the API token JWT is the
api_key secret; the Platform Manager gateway base URL lives in extra_config).
LogRhythm is a required-``base_url`` provider, so the contract guarded here:
the configured base_url is stripped of surrounding whitespace before it is
embedded in the compound token. A whitespace-only value must collapse to the
empty-base_url warning path (returning ``||token``) instead of silently
embedding the spaces (``   ||token``), which would produce an unusable URL and
hide the misconfiguration - matching the connectivity test, which also strips.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.integrations.http import parse_compound_token

_TOKEN = "lr-api-token-jwt-secret-xyz"


class _FakeIntegration:
    def __init__(self, *, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "logrhythm-prod"
        self.provider = "logrhythm"
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
    """Invoke ``_get_token`` for a LogRhythm integration whose ``extra_config``
    dict the test controls.
    """
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: _TOKEN)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: _TOKEN)

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

            async def _reveal_extra(_integ, _e=extra):
                return dict(_e)

            # The tools read extra_config through the vault resolver (#6080).
            monkeypatch.setattr(integration_tools, "_reveal_extra", _reveal_extra)
            integ = _FakeIntegration(extra_config_enc=b"blob")
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider="logrhythm",
        )

    return _runner


@pytest.mark.asyncio
async def test_full_base_url_returns_compound(_resolve):
    token = await _resolve({"base_url": "https://lr-pm.example.com:8501/"})
    # trailing slash stripped; segments are base_url||api_token
    assert token == f"https://lr-pm.example.com:8501||{_TOKEN}"
    base_url, api_token = parse_compound_token(token, 2)
    assert base_url == "https://lr-pm.example.com:8501"
    assert api_token == _TOKEN


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_stripped(_resolve):
    token = await _resolve({"base_url": "  https://lr-pm.example.com:8501  "})
    base_url, api_token = parse_compound_token(token, 2)
    assert base_url == "https://lr-pm.example.com:8501"
    assert api_token == _TOKEN


@pytest.mark.asyncio
async def test_whitespace_only_base_url_collapses_to_empty(_resolve):
    # a whitespace-only base_url must NOT embed the spaces ("   ||token"); it
    # strips to empty so the warning path returns the clean partial token
    token = await _resolve({"base_url": "   "})
    assert token == f"||{_TOKEN}"
    base_url, api_token = parse_compound_token(token, 2)
    assert base_url == ""
    assert api_token == _TOKEN
