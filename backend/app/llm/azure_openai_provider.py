# SPDX-License-Identifier: Apache-2.0

"""Azure OpenAI LLM provider.

Azure OpenAI Service provides access to OpenAI models through Azure's
infrastructure.  Uses the OpenAI SDK with Azure-specific configuration.

The API key format is: ``endpoint||api_key||api_version``
- endpoint: Azure resource endpoint (e.g. ``https://myresource.openai.azure.com``)
- api_key: Azure API key
- api_version: API version (e.g. ``2024-10-21``)

If only a plain API key is provided, defaults are used for endpoint and version.
"""

from __future__ import annotations

import base64
import logging

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.core.utils import format_exception_message
from app.llm.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

# Default Azure API version
DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure OpenAI Service — enterprise OpenAI models on Azure."""

    provider_name = "azure_openai"
    default_model = "gpt-4o"
    base_url = None  # Handled by AzureOpenAI client

    def __init__(self, api_key: str, **kwargs):
        # Deliberately does NOT call ``super().__init__`` — the base
        # constructor's only job is building a plain ``AsyncOpenAI`` client,
        # which would (a) receive the raw compound ``endpoint||key||version``
        # string as its API key and (b) be immediately orphaned (never
        # closed) when replaced by the Azure client. Construct the single
        # correct client directly instead.
        parts = api_key.split("||")
        if len(parts) >= 2:
            endpoint = parts[0].strip()
            actual_key = parts[1].strip()
            api_version = parts[2].strip() if len(parts) > 2 else DEFAULT_API_VERSION
        else:
            # Fallback: assume plain key with default endpoint
            endpoint = "https://openai.azure.com"
            actual_key = api_key
            api_version = DEFAULT_API_VERSION

        self.client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=actual_key,
            api_version=api_version,
        )
        self._endpoint = endpoint.rstrip("/")
        self._key = actual_key
        self._v1_client: AsyncOpenAI | None = None

    def _videos_client(self) -> AsyncOpenAI:
        """Azure serves the Videos API (Sora deployments) on its OpenAI v1 endpoint, not per deployment."""
        if self._v1_client is None:
            self._v1_client = AsyncOpenAI(base_url=f"{self._endpoint}/openai/v1/", api_key=self._key)
        return self._v1_client

    # Its speech deployments are OpenAI's models, so they speak OpenAI's voices.
    speech_voices_are_fixed = True

    def _speech_client(self) -> AsyncOpenAI:
        """The speech API of Azure's OpenAI v1 endpoint, where the model is the deployment's name."""
        return self._videos_client()

    async def list_models(self) -> list[str]:
        """Fetch available deployments from Azure OpenAI."""
        try:
            response = await self.client.models.list()
            return sorted([m.id for m in response.data], reverse=True)
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception:
            return [
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4-turbo",
                "gpt-4",
                "gpt-35-turbo",
            ]

    async def _listed_model_ids(self, capability: str) -> list[str]:
        """The ids of the resource's model listing; empty (and logged) when it cannot be read."""
        try:
            response = await self.client.models.list()
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception as exc:
            logger.warning(
                "Azure OpenAI model listing failed while discovering %s models: %s",
                capability,
                format_exception_message(exc),
            )
            return []
        return [m.id for m in response.data]

    async def list_embedding_models(self) -> list[str]:
        """The listed models the model catalog knows as embedding models (a deployment is named after its model)."""
        from app.llm.router import is_embedding_model

        return sorted((m for m in await self._listed_model_ids("embedding") if is_embedding_model(m)), reverse=True)

    async def list_image_models(self) -> list[str]:
        """The listed models the model catalog knows as image generation models."""
        from app.llm.router import is_image_model

        return sorted((m for m in await self._listed_model_ids("image") if is_image_model(m)), reverse=True)

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """An embedding from the deployment *model* (the Embeddings API of the Azure resource)."""
        from app.llm.router import EMBEDDING_DEFAULTS

        model = model or EMBEDDING_DEFAULTS["azure_openai"]
        response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """An image from the deployment *model* (the Images API of the Azure resource), as raw bytes.

        Azure has no default image model: its ids are the deployment names an
        administrator chose. Only a DALL-E deployment takes ``response_format``;
        the GPT image models always answer in base64 and refuse the parameter.
        """
        if not model:
            raise ValueError("Azure OpenAI image generation needs an image deployment chosen as the image model.")
        params: dict = {"model": model, "prompt": prompt, "size": size, "n": 1}
        if model.startswith("dall-e-"):
            params["response_format"] = "b64_json"
        response = await self.client.images.generate(**params)
        encoded = response.data[0].b64_json if response.data else None
        if not encoded:
            raise RuntimeError(f"Azure OpenAI deployment {model[:100]!r} returned no image.")
        return base64.b64decode(encoded)
