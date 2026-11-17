# SPDX-License-Identifier: Apache-2.0

"""SpaceXAI (Grok) LLM provider.

SpaceXAI (formerly xAI, https://docs.x.ai) serves the Grok model family
through an OpenAI-compatible API endpoint at ``https://api.x.ai/v1``.

Supports tool calling, streaming, reasoning content, and prompt caching.
Chat-only: SpaceXAI does not expose an embeddings API, so knowledge bases
still need an embedding-capable provider alongside it.
"""

from __future__ import annotations

import openai

from app.llm.openai_compat import OpenAICompatibleProvider

# The SpaceXAI catalog also lists image/video generation ("grok-imagine-*")
# and voice models (text-to-speech / speech-to-text); none of them serve
# ``chat.completions``, so they are filtered out of chat model discovery.
_NON_CHAT_MARKERS = ("imagine", "image", "voice", "stt", "tts", "embed", "moderation")


class XAIProvider(OpenAICompatibleProvider):
    """SpaceXAI — Grok frontier models (chat, tools, reasoning)."""

    provider_name = "xai"
    # Conservative class-wide floor: every current Grok chat model has at
    # least a 256k window (grok-build; grok-4.6/4.5 are 500k, grok-4.3 is
    # 1M). The model_limits family table and the metadata snapshot refine
    # this per model.
    context_window = 256_000
    max_output_tokens = 16_384
    default_model = "grok-4.6"
    base_url = "https://api.x.ai/v1"

    async def list_models(self) -> list[str]:
        """Fetch available models from SpaceXAI, keeping chat-capable ones."""
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
                "grok-4.7",
                "grok-4.6",
                "grok-4.5",
                "grok-4.3",
                "grok-build-0.1",
            ]
