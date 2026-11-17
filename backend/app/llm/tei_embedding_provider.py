# SPDX-License-Identifier: Apache-2.0

"""Embedding server provider (``POST /embed``).

This provider targets embedding endpoints that expose a native
``POST /embed`` route. It is embedding-only and does not implement chat
completion APIs.

Credential format:
- ``api_key`` field stores ``token||base_url``
  - token: bearer token (use ``none`` or ``dummy`` when auth is not required)
    - base_url: embedding endpoint base URL (for example
        ``https://embedder.example.com``)
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.base import ToolDefinition


class TEIEmbeddingProvider(BaseLLMProvider):
    """Embedding-only provider for ``/embed``-compatible endpoints."""

    provider_name = "tei"
    only_capability = "embedding"
    default_model = "tei-embedding"
    base_url = "http://localhost:8080"

    def __init__(self, api_key: str, **kwargs):
        del kwargs
        parts = api_key.split("||")
        token = parts[0].strip() if parts else ""
        self._token = "" if token.lower() in {"none", "dummy"} else token
        if len(parts) >= 2:
            base = parts[1].strip().rstrip("/")
            if base:
                self.base_url = base

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs,
    ) -> ChatResponse:
        del messages, model, temperature, max_tokens, tools, kwargs
        raise NotImplementedError("TEI provider supports embeddings only.")

    async def list_models(self) -> list[str]:
        """TEI does not expose a standard model discovery route."""
        return []

    async def list_embedding_models(self) -> list[str]:
        """Model discovery is not guaranteed on TEI native endpoints."""
        return []

    @staticmethod
    def _extract_vector(payload: object) -> list[float]:
        def _is_number_list(value: object) -> bool:
            return isinstance(value, Sequence) and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
            )

        # TEI single-input response: [float, float, ...]
        if _is_number_list(payload):
            return [float(v) for v in payload]

        # Some deployments may return batched output: [[...], [...]]
        if (
            isinstance(payload, Sequence)
            and payload
            and isinstance(payload[0], Sequence)
            and _is_number_list(payload[0])
        ):
            return [float(v) for v in payload[0]]

        # Compatibility shape: {"embeddings": [[...]]} or {"embedding": [...]}
        if isinstance(payload, dict):
            embedding = payload.get("embedding")
            if _is_number_list(embedding):
                return [float(v) for v in embedding]

            embeddings = payload.get("embeddings")
            if _is_number_list(embeddings):
                return [float(v) for v in embeddings]
            if (
                isinstance(embeddings, Sequence)
                and embeddings
                and isinstance(embeddings[0], Sequence)
                and _is_number_list(embeddings[0])
            ):
                return [float(v) for v in embeddings[0]]

        raise ValueError(
            "Unexpected TEI embedding response format. Expected a numeric vector."
        )

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Create an embedding using TEI's native ``POST /embed`` route."""
        del model
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
        ) as client:
            response = await client.post(
                f"{self.base_url}/embed",
                headers=headers,
                json={"inputs": text},
            )
            response.raise_for_status()
            payload = response.json()

        return self._extract_vector(payload)
