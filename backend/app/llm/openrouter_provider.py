# SPDX-License-Identifier: Apache-2.0

"""OpenRouter LLM provider — access hundreds of models via a single API key.

OpenRouter (https://openrouter.ai) provides a unified API compatible with
OpenAI's format, giving access to Claude, GPT, Gemini, Llama, Mistral,
and many more models through a single endpoint and API key.

This is the recommended provider for teams that want maximum model
flexibility without configuring individual provider API keys.
"""

from __future__ import annotations

import logging

import openai

from app.core.utils import format_exception_message
from app.llm.decisions import DECISIONS_SYSTEM_ONE
from app.llm.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

#: Longest model id kept from OpenRouter's embedding catalog (its ids are ``vendor/name``).
_MAX_MODEL_ID_LENGTH = 200


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter — unified multi-model provider."""

    provider_name = "openrouter"
    context_window = 1_000_000
    max_output_tokens = 8_192
    default_model = "anthropic/claude-sonnet-5"
    base_url = "https://openrouter.ai/api/v1"
    # Decision models (Jev, ...) are served on OpenRouter's System One
    # surface, ``{base_url}/systemone``.
    decisions_api = DECISIONS_SYSTEM_ONE

    async def list_embedding_models(self) -> list[str]:
        """The embedding models of OpenRouter's catalog (``GET /embeddings/models``), read with the provider's client."""
        import httpx

        try:
            response = await self.client.get(
                "embeddings/models", cast_to=httpx.Response, options={"timeout": 10, "max_retries": 0}
            )
            entries = response.json().get("data")
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except (openai.APIError, ValueError, AttributeError) as exc:
            logger.warning("OpenRouter embedding model listing failed: %s", format_exception_message(exc))
            return []
        if not isinstance(entries, list):
            return []
        return sorted(
            {
                entry["id"]
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and 0 < len(entry["id"]) <= _MAX_MODEL_ID_LENGTH
            }
        )

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """An embedding from OpenRouter's ``/embeddings`` route; the model is chosen in Settings (no default)."""
        if not model:
            raise ValueError("OpenRouter embeddings need an embedding model chosen in Settings.")
        response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    async def list_models(self) -> list[str]:
        """Fetch available models from OpenRouter."""
        try:
            response = await self.client.models.list()
            models: list[str] = []
            for m in response.data:
                model_id = m.id
                # Skip the rate-limited ":free" variants — they duplicate the
                # paid model ids and are unusable for production traffic.
                if ":free" in model_id:
                    continue
                models.append(model_id)
            models.sort()
            return models
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception:
            # Fallback popular models
            return [
                "anthropic/claude-fable-5",
                "anthropic/claude-opus-5",
                "anthropic/claude-sonnet-5",
                "anthropic/claude-haiku-4.5",
                "openai/gpt-6-astra",
                "openai/gpt-5.6",
                "openai/gpt-4o",
                "google/gemini-3.8-flash",
                "x-ai/grok-4.6",
                "meta-llama/llama-3.3-70b-instruct",
                "mistralai/mistral-large",
            ]
