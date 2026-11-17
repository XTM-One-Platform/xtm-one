# SPDX-License-Identifier: Apache-2.0

"""LiteLLM proxy provider — unified interface to 100+ LLMs.

LiteLLM (https://docs.litellm.ai) is an open-source LLM gateway that
provides a unified OpenAI-compatible API for 100+ model providers.

The API key format is: ``api_key||base_url``
- If only an API key is provided, uses the default LiteLLM cloud proxy.
- If a URL is provided, connects to a self-hosted LiteLLM proxy.

Common base URLs:
- Self-hosted: ``http://localhost:4000``
- LiteLLM cloud: ``https://api.litellm.ai``
"""

from __future__ import annotations

import openai

from app.llm.openai_compat import OpenAICompatibleProvider


class LiteLLMProvider(OpenAICompatibleProvider):
    """LiteLLM — unified proxy for 100+ LLM providers."""

    provider_name = "litellm"
    default_model = "gpt-4o"
    base_url = "http://localhost:4000/v1"  # Default self-hosted

    def __init__(self, api_key: str, **kwargs):
        # Parse compound key: api_key||base_url
        parts = api_key.split("||")
        actual_key = parts[0].strip()
        if len(parts) >= 2:
            base = parts[1].strip().rstrip("/")
            if not base.endswith("/v1"):
                base += "/v1"
            self.base_url = base

        super().__init__(api_key=actual_key, **kwargs)

    async def _declared_models(self, mode: str) -> set[str]:
        """The aliases the proxy's ``/model/info`` declares in *mode* (``video_generation``, ``audio_speech``).

        A LiteLLM proxy lists the aliases its admin chose (``veo``, ``narrator``),
        which say nothing on their own; its model info carries each alias's
        mode. Read with the provider's own client, so it goes where and how
        every other call of this provider goes.
        """
        import httpx

        try:
            response = await self.client.get(
                "model/info", cast_to=httpx.Response, options={"timeout": 10, "max_retries": 0}
            )
            entries = response.json().get("data") or []
        except (openai.APIError, ValueError, AttributeError):
            entries = []
        declared: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            info = entry.get("model_info") or {}
            if info.get("mode") == mode and entry.get("model_name"):
                declared.add(str(entry["model_name"]))
        return declared

    async def list_video_models(self) -> list[str]:
        """The proxy's video models, by its model info, else by id (the OpenAI Videos API, ``app.llm.video``)."""
        return sorted(await self._declared_models("video_generation") | set(await super().list_video_models()))

    async def list_speech_models(self) -> list[str]:
        """The proxy's speech models, by its model info, else by id (the OpenAI speech API, ``app.llm.speech``).

        Amazon Polly, Azure Speech, Vertex AI Chirp, ElevenLabs or MiniMax
        behind the proxy are spoken through the same route, with their own voices.
        """
        return sorted(await self._declared_models("audio_speech") | set(await super().list_speech_models()))

    async def list_embedding_models(self) -> list[str]:
        """The proxy's embedding models, by its model info (the OpenAI Embeddings API it proxies)."""
        return sorted(await self._declared_models("embedding"))

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """An embedding from the proxy's ``/embeddings`` route for the alias *model*.

        A proxy serves the aliases its administrator configured, so there is no
        default embedding model: one is chosen in Settings.
        """
        if not model:
            raise ValueError("LiteLLM embeddings need an embedding model chosen in Settings.")
        response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    async def list_models(self) -> list[str]:
        """Fetch models from the LiteLLM proxy."""
        try:
            response = await self.client.models.list()
            return sorted([m.id for m in response.data])
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception:
            # LiteLLM proxy may not be running yet
            return [
                "gpt-6-astra",
                "gpt-4o",
                "claude-sonnet-5",
                "gemini-3.8-flash",
            ]
