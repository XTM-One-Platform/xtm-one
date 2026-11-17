# SPDX-License-Identifier: Apache-2.0

"""Custom OpenAI-compatible endpoint provider.

Connects to any API that implements the OpenAI chat completions format.
This covers self-hosted solutions like vLLM, text-generation-inference (TGI),
LocalAI, LM Studio, FastChat, Aphrodite, TabbyAPI, and any other
OpenAI-compatible server. Embeddings are supported when the endpoint exposes
an OpenAI-compatible embeddings route (for example ``POST /v1/embeddings``
or ``POST /embeddings``) and an embedding model is selected.

The API key format is: ``api_key||base_url``
- ``api_key`` — authentication key (use ``none`` or ``dummy`` if not required)
- ``base_url`` — full base URL of the OpenAI-compatible endpoint

Examples:
- ``sk-my-key||https://my-vllm-server.example.com/v1``
- ``none||http://localhost:8000``
- ``dummy||http://localai:8080``
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import httpx

from app.llm.decisions import DECISIONS_OPENAI
from app.llm.openai_compat import OpenAICompatibleProvider, declared_model_types


class _EmbeddingsClient(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class CustomOpenAIProvider(OpenAICompatibleProvider):
    """Custom OpenAI-compatible endpoint — bring your own server."""

    provider_name = "custom_openai"
    default_model = "default"
    # Taken on the administrator's word: an endpoint that serves OpenAI's
    # Decisions route (the Filigran AI Gateway) types its decision models as
    # such, any other lists none.
    decisions_api = DECISIONS_OPENAI
    decisions_only_when_listed = True
    lists_decision_models = True
    base_url = "http://localhost:8000/v1"  # placeholder default

    def __init__(self, api_key: str, **kwargs):
        # Parse compound key: api_key||base_url
        parts = api_key.split("||")
        actual_key = parts[0].strip()
        if len(parts) >= 2:
            base = parts[1].strip().rstrip("/")
            self.base_url = base

        super().__init__(api_key=actual_key, **kwargs)

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        api_key = self.client.api_key
        if api_key and api_key.lower() not in ("none", "dummy"):
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _embeddings_client(self) -> _EmbeddingsClient:
        return cast(_EmbeddingsClient, self.client.embeddings)

    async def _list_model_entries(self) -> list[dict]:
        """The ``/models`` entries over raw HTTP, to tolerate non-standard fields."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10),
        ) as http:
            resp = await http.get(
                f"{self.base_url}/models",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            body = resp.json()

        data = body.get("data", []) if isinstance(body, dict) else []
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    async def _list_model_ids(self) -> list[str]:
        return sorted(m["id"] for m in await self._list_model_entries() if "id" in m)

    async def list_models(self) -> list[str]:
        """Fetch models via raw HTTP to tolerate non-standard response fields."""
        return await self._list_model_ids()

    async def list_decision_models(self) -> list[str]:
        """The models whose declared types include ``decision`` (``types`` or ``type``, as the gateway lists them)."""
        return sorted(
            m["id"]
            for m in await self._list_model_entries()
            if "id" in m and "decision" in declared_model_types(m.get("types"), m.get("type"))
        )

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Create embeddings via the OpenAI-compatible embeddings API.

        Passes ``dimensions=<platform_configured_dimensions>`` to activate Matryoshka
        truncation on models that support it (e.g. nomic-embed-text-v1.5).
        Falls back to a call without ``dimensions`` when the endpoint rejects
        that parameter (older or non-standard servers).

        Raises ``ValueError`` when the returned vector length does not match
        the platform-configured dimensions, because mismatched dimensions would silently
        corrupt the pgvector index.
        """
        if not model:
            raise ValueError(
                "custom_openai embeddings require an explicit embedding model."
            )

        from app.models.embedding import EMBEDDING_DIMENSIONS

        expected_dimensions = self.embedding_dimensions or EMBEDDING_DIMENSIONS
        embeddings_client = self._embeddings_client()

        try:
            response = await embeddings_client.create(
                input=text, model=model, dimensions=expected_dimensions
            )
        except Exception as exc:
            # Some endpoints (vLLM < 0.4, LocalAI, …) reject unknown params.
            # Retry without dimensions and let the caller decide whether the
            # native output size is acceptable.
            msg = str(exc).lower()
            if "dimension" in msg or "unexpected keyword argument" in msg:
                response = await embeddings_client.create(
                    input=text, model=model
                )
            else:
                raise

        vector = response.data[0].embedding
        if len(vector) != expected_dimensions:
            raise ValueError(
                f"Embedding model '{model}' returned {len(vector)} dimensions but "
                f"the platform is configured for {expected_dimensions}. "
                f"Update Settings → AI and models → Models → Vector Dimensions to {len(vector)}, or choose "
                "a model that produces the expected number of dimensions."
            )
        return vector

    async def probe_supported_dimensions(
        self,
        text: str,
        *,
        model: str,
        candidates: tuple[int, ...],
    ) -> tuple[list[float], int | None]:
        """Probe embeddings across candidate dimensions and return first success.

        Candidates should be passed in **descending** order (highest first) so
        the best supported dimension is selected.  Returns
        ``(vector, requested_dimensions)`` where ``requested_dimensions`` is the
        candidate that succeeded, or ``None`` when only the dimensionless
        fallback call succeeded.
        """
        last_error: Exception | None = None

        for dim in candidates:
            try:
                response = await self._embeddings_client().create(
                    input=text,
                    model=model,
                    dimensions=dim,
                )
                return response.data[0].embedding, dim
            except Exception as exc:  # pragma: no cover - provider-specific errors
                last_error = exc

        try:
            response = await self._embeddings_client().create(input=text, model=model)
            return response.data[0].embedding, None
        except Exception as exc:
            if last_error is not None:
                raise last_error from exc
            raise

    async def list_embedding_models(self) -> list[str]:
        """Return likely embedding models from the provider's ``/models`` list.

        Uses a marker-based heuristic to identify embedding models.  The
        admin must still explicitly select a model in Settings, so false
        positives from short markers (e.g. ``e5``) are acceptable.
        """
        embedding_markers = ("embedding", "embed", "bge", "e5", "gte")
        try:
            model_ids = await self._list_model_ids()
        except Exception:
            return []

        return [
            model_id
            for model_id in model_ids
            if any(marker in model_id.lower() for marker in embedding_markers)
        ]
