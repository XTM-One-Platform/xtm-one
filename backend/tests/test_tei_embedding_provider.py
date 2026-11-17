"""Tests for the TEI embedding provider."""

import asyncio
from typing import Any, cast

from app.llm.tei_embedding_provider import TEIEmbeddingProvider


class _FakeResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: object):
        self._payload = payload
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_json: dict[str, object] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return _FakeResponse(self._payload)


def test_tei_provider_parses_native_vector_response(monkeypatch):
    provider = TEIEmbeddingProvider(api_key="hf_test||https://embedder.example.com")
    fake_client = _FakeAsyncClient([0.1, 0.2, 0.3])

    monkeypatch.setattr(
        "app.llm.tei_embedding_provider.httpx.AsyncClient",
        lambda *args, **kwargs: cast(Any, fake_client),
    )

    vector = asyncio.run(provider.embed("Hello world"))

    assert vector == [0.1, 0.2, 0.3]
    assert fake_client.last_url == "https://embedder.example.com/embed"
    assert fake_client.last_json == {"inputs": "Hello world"}
    assert fake_client.last_headers is not None
    assert fake_client.last_headers.get("Authorization") == "Bearer hf_test"


def test_tei_provider_supports_no_auth_token(monkeypatch):
    provider = TEIEmbeddingProvider(api_key="none||https://embedder.example.com")
    fake_client = _FakeAsyncClient([[0.4, 0.5]])

    monkeypatch.setattr(
        "app.llm.tei_embedding_provider.httpx.AsyncClient",
        lambda *args, **kwargs: cast(Any, fake_client),
    )

    vector = asyncio.run(provider.embed("Hello world"))

    assert vector == [0.4, 0.5]
    assert fake_client.last_headers is not None
    assert "Authorization" not in fake_client.last_headers


def test_tei_provider_rejects_unexpected_payload(monkeypatch):
    provider = TEIEmbeddingProvider(api_key="dummy||https://embedder.example.com")
    fake_client = _FakeAsyncClient({"unexpected": "shape"})

    monkeypatch.setattr(
        "app.llm.tei_embedding_provider.httpx.AsyncClient",
        lambda *args, **kwargs: cast(Any, fake_client),
    )

    try:
        asyncio.run(provider.embed("Hello world"))
    except ValueError as exc:
        assert "Unexpected TEI embedding response format" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid TEI response payload")
