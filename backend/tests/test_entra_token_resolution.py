"""Regression tests for Microsoft Entra ID app-only compound-token resolution
in ``integration_tools._get_token``.

App-only (API Key) Entra integrations assemble the compound
``tenant_id||client_id||client_secret`` token that ``EntraClient`` mints its own
Microsoft Graph tokens from. The decrypted ``client_id`` / ``client_secret`` are
read from the dedicated encrypted columns and **must be stripped** before being
joined: a secret copy/pasted into the column commonly carries a trailing
newline, which would otherwise be sent verbatim to the client-credentials grant
and fail in a non-obvious way.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools


class _FakeIntegration:
    def __init__(
        self,
        *,
        auth_type: str = "api_key",
        client_id_enc: bytes | None = b"cid-blob",
        client_secret_enc: bytes | None = b"csec-blob",
        extra_config_enc: bytes | None = b"extra-blob",
    ):
        self.id = uuid.uuid4()
        self.name = "entra-prod"
        self.provider = "entra"
        self.auth_type = auth_type
        self.access_token_enc = None
        self.api_key_enc = None
        self.client_id_enc = client_id_enc
        self.client_secret_enc = client_secret_enc
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
    """Invoke ``_get_token`` for an app-only Entra integration whose decrypted
    credentials and ``extra_config`` the test controls.
    """

    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(
        *, client_id: str, client_secret: str, extra: dict[str, Any]
    ) -> str | None:
        creds = {b"cid-blob": client_id, b"csec-blob": client_secret}
        monkeypatch.setattr(
            integration_tools, "decrypt_value", lambda blob: creds[blob]
        )

        async def _reveal(entity, column):
            blob = getattr(entity, column)
            return creds[blob] if blob else None

        monkeypatch.setattr(integration_tools, "reveal_field", _reveal)
        monkeypatch.setattr(
            integration_tools, "decrypt_json", lambda _blob, _e=extra: dict(_e)
        )

        async def _reveal_extra(_integ, _e=extra):
            return dict(_e)

        # The tools read extra_config through the vault resolver (#6080).
        monkeypatch.setattr(integration_tools, "_reveal_extra", _reveal_extra)
        integ = _FakeIntegration()
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider="entra",
        )

    return _runner


@pytest.mark.asyncio
async def test_entra_app_only_strips_decrypted_credentials(_resolve):
    # A client id/secret copy/pasted with surrounding whitespace/newlines must be
    # stripped so the compound token (and the downstream token mint) is clean.
    token = await _resolve(
        client_id="  client-1\n",
        client_secret="secret-1\r\n",
        extra={"tenant_id": "  tenant-1 "},
    )
    assert token == "tenant-1||client-1||secret-1"


@pytest.mark.asyncio
async def test_entra_app_only_whitespace_only_secret_is_missing(_resolve):
    # A credential that is only whitespace strips to empty and is treated as
    # missing (None), not joined into a malformed token.
    token = await _resolve(
        client_id="client-1",
        client_secret="   \n",
        extra={"tenant_id": "tenant-1"},
    )
    assert token is None
