# SPDX-License-Identifier: Apache-2.0

"""Mistral AI LLM provider.

Mistral AI (https://mistral.ai) provides high-performance models with
an OpenAI-compatible API endpoint at ``https://api.mistral.ai/v1``.

Supports tool calling, streaming, and embeddings.
"""

from __future__ import annotations

import openai

from app.llm.openai_compat import OpenAICompatibleProvider

# The Mistral catalog also lists embedding, moderation, OCR, transcription and
# speech models; none of them serve ``chat.completions``.
_NON_CHAT_MARKERS = ("embed", "moderation", "ocr", "transcribe", "tts")


class MistralProvider(OpenAICompatibleProvider):
    """Mistral AI — high-performance European AI models."""

    provider_name = "mistral"
    # Floors for a model the model_limits family table does not list: the
    # smallest current window, and an output that fits beside a prompt
    # compaction lets grow to 80% of it.
    context_window = 128_000
    max_output_tokens = 16_384
    default_model = "mistral-large-latest"
    base_url = "https://api.mistral.ai/v1"

    async def list_models(self) -> list[str]:
        """Fetch available models from Mistral AI."""
        try:
            response = await self.client.models.list()
            models: list[str] = []
            for m in response.data:
                model_id = m.id
                if any(kw in model_id for kw in _NON_CHAT_MARKERS):
                    continue
                models.append(model_id)
            models.sort(reverse=True)
            return models
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception:
            return [
                "mistral-large-latest",
                "mistral-medium-latest",
                "mistral-small-latest",
                "magistral-medium-latest",
                "codestral-latest",
                "ministral-8b-latest",
            ]

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Mistral supports embeddings."""
        from app.llm.router import EMBEDDING_DEFAULTS

        model = model or EMBEDDING_DEFAULTS.get("mistral", "mistral-embed")
        response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    async def list_embedding_models(self) -> list[str]:
        """Return Mistral embedding models."""
        return ["mistral-embed"]
