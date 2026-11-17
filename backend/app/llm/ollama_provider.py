# SPDX-License-Identifier: Apache-2.0

"""Ollama LLM provider — self-hosted local models.

Ollama (https://ollama.ai) runs LLMs locally and exposes an
OpenAI-compatible API.  This provider connects to the Ollama server
(default: ``http://localhost:11434``) using the OpenAI SDK.

The API key is used to configure the Ollama server URL. Pass the
server URL as the ``api_key`` field when creating the provider config.
If the key looks like a URL, it's used as the base URL; otherwise
the default ``http://localhost:11434`` is used.

Example configs:
- api_key = "http://localhost:11434"  → local Ollama
- api_key = "http://gpu-server:11434" → remote Ollama
- api_key = "ollama"                  → uses default localhost
"""

from __future__ import annotations

import logging

from app.llm.decisions import DECISIONS_SYSTEM_ONE
from app.llm.model_limits import reported_context_window
from app.llm.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama — self-hosted local models with OpenAI-compatible API."""

    provider_name = "ollama"
    max_output_tokens = 4_096
    # Local models typically have smaller context windows; keep a
    # conservative tool cap to avoid degraded tool selection quality.
    max_tools = 64
    default_model = "llama3.2"
    base_url = "http://localhost:11434/v1"
    # Decision models (Clef) are served on Ollama's System One surface,
    # ``{base_url}/systemone``.
    decisions_api = DECISIONS_SYSTEM_ONE
    # /api/tags reports each local model's ``decision`` capability.
    lists_decision_models = True

    def __init__(self, api_key: str, **kwargs):
        # The api_key field is repurposed as the server URL for Ollama
        if api_key.startswith(("http://", "https://")):
            base = api_key.rstrip("/")
            if not base.endswith("/v1"):
                base += "/v1"
            self.base_url = base
        super().__init__(api_key="ollama", **kwargs)

    async def list_models(self) -> list[str]:
        """Fetch available models from the Ollama server."""
        response = await self.client.models.list()
        return sorted([m.id for m in response.data])

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Ollama supports embeddings for some models."""
        from app.llm.router import EMBEDDING_DEFAULTS

        model = model or EMBEDDING_DEFAULTS.get("ollama", "nomic-embed-text")
        response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    async def list_embedding_models(self) -> list[str]:
        """Return common Ollama embedding models."""
        return ["nomic-embed-text", "mxbai-embed-large", "all-minilm"]

    async def list_decision_models(self) -> list[str]:
        """Models whose ``capabilities`` include ``decision``; an unreachable Ollama raises.

        A failure is not an empty listing: the flow Classify node must not
        read a down local provider as one that serves no decision model.
        """
        root = self.base_url.removesuffix("/v1")
        tags = await self.client.get(f"{root}/api/tags", cast_to=object)
        return sorted(
            m["name"]
            for m in (tags or {}).get("models", [])
            if "decision" in (m.get("capabilities") or [])
        )

    async def decision_context_window(self, model: str) -> int | None:
        """The administrator's window, else the ``num_ctx`` Ollama runs *model* with.

        Ollama never cuts a decision's input: a question longer than
        ``num_ctx`` is refused. A model without the parameter runs with the
        server's own default, which the API does not report.
        """
        if window := await super().decision_context_window(model):
            return window
        root = self.base_url.removesuffix("/v1")
        try:
            show = await self.client.post(f"{root}/api/show", body={"model": model}, cast_to=object)
        except Exception as exc:
            logger.warning("Ollama model details for %.200r failed: %s", model, exc)
            return None
        parameters = (show or {}).get("parameters") if isinstance(show, dict) else None
        for line in (parameters or "").splitlines():
            name, _, value = line.strip().partition(" ")
            if name == "num_ctx" and value.strip().isdigit():
                return reported_context_window(int(value.strip()))
        return None
