"""Regression tests for the Notion OAuth token-exchange request shape.

Notion's ``/v1/oauth/token`` endpoint is non-standard: it requires HTTP Basic
Authentication (``Authorization: Basic base64(client_id:client_secret)``) with a
**JSON** body, and rejects the credentials-in-body / form-encoded style every
other OAuth 2.0 provider uses (returning ``401 invalid_client``).

Because the original breakage was *request-shape* specific, these tests pin the
exact shape ``exchange_code_for_tokens_with_integration`` sends:

- Notion -> ``json=`` body with ``grant_type``/``code``/``redirect_uri`` only,
  a ``Basic`` ``Authorization`` header, and **no** ``client_id`` /
  ``client_secret`` in the body and **no** ``data=`` form.
- Any other provider -> the unchanged form-encoded path (``data=`` with the
  credentials in the body and **no** ``Authorization`` header).
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.integrations import oauth


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Records the single ``post`` call so the request shape can be asserted."""

    def __init__(self, calls: list[dict], payload: dict):
        self._calls = calls
        self._payload = payload

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self._calls.append({"url": url, "kwargs": kwargs})
        return _FakeResponse(self._payload)


def _patch_client(monkeypatch, calls: list[dict], payload: dict) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(calls, payload),
    )
    # Avoid needing a Fernet key / real ciphertext: treat the *_enc fields as
    # already-decrypted plaintext for the test.
    monkeypatch.setattr(oauth, "decrypt_value", lambda v: v)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda v: v)


@pytest.mark.asyncio
async def test_notion_uses_basic_auth_and_json_body(monkeypatch):
    calls: list[dict] = []
    payload = {"access_token": "notion-token", "workspace_id": "ws-1"}
    _patch_client(monkeypatch, calls, payload)

    integration = SimpleNamespace(
        provider="notion",
        client_id_enc="notion-client-id",
        client_secret_enc="notion-client-secret",
        token_url="https://api.notion.com/v1/oauth/token",
        extra_config_enc=None,
    )

    result = await oauth.exchange_code_for_tokens_with_integration(
        integration, code="auth-code-123"
    )

    assert result == payload
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.notion.com/v1/oauth/token"

    # JSON body — only the standard fields, never the credentials.
    body = call["kwargs"]["json"]
    assert body == {
        "grant_type": "authorization_code",
        "code": "auth-code-123",
        "redirect_uri": f"{settings.base_url}/api/oauth/callback",
    }
    assert "client_id" not in body
    assert "client_secret" not in body
    # Form body must NOT be used for Notion.
    assert "data" not in call["kwargs"]

    # Basic auth header = base64("client_id:client_secret").
    expected_basic = base64.b64encode(
        b"notion-client-id:notion-client-secret"
    ).decode("ascii")
    headers = call["kwargs"]["headers"]
    assert headers["Authorization"] == f"Basic {expected_basic}"
    assert headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_non_notion_uses_form_body_with_credentials(monkeypatch):
    """The generic path is unchanged: form body with credentials, no Basic auth."""
    calls: list[dict] = []
    payload = {"access_token": "gh-token"}
    _patch_client(monkeypatch, calls, payload)

    integration = SimpleNamespace(
        provider="github",
        client_id_enc="gh-client-id",
        client_secret_enc="gh-client-secret",
        token_url="https://github.com/login/oauth/access_token",
        extra_config_enc=None,
    )

    result = await oauth.exchange_code_for_tokens_with_integration(
        integration, code="auth-code-456"
    )

    assert result == payload
    assert len(calls) == 1
    call = calls[0]

    # Generic providers post a form body with the credentials inline …
    data = call["kwargs"]["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "auth-code-456"
    assert data["client_id"] == "gh-client-id"
    assert data["client_secret"] == "gh-client-secret"
    # … and never a JSON body or a Basic auth header.
    assert "json" not in call["kwargs"]
    assert "Authorization" not in call["kwargs"]["headers"]
