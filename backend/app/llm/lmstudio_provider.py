# SPDX-License-Identifier: Apache-2.0

"""LM Studio LLM provider - models served by an LM Studio server, fed by its integration.

LM Studio serves an OpenAI-compatible API (``/v1/chat/completions``,
``/v1/embeddings``, ``/v1/models``) next to its native REST API
(``/api/v1/models`` lists each model's type). This provider is only ever fed
by an LM Studio integration (``integration_fed_only``): it holds no URL and
no key of its own. Its HTTP client sends every request through
``services/lmstudio_llm_provider.IntegrationTransport``, which reads the
server URL, token and relay group of the integration feeding its config,
sends the request through that relay group or, without one, directly, and
refuses (424, never retried) when that integration cannot carry it.

Chat, streaming, tools and structured output are the OpenAI-compatible base
class's; embeddings and the dimension probe are the custom endpoint's. The
model listings read the native API, which says which models chat, embed or
decide, rather than guessing from model names.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.openai_compat import OpenAICompatibleProvider

_TIMEOUT = httpx.Timeout(connect=10, read=600, write=30, pool=10)
#: A listing of a few hundred models is well under 1 MB.
_MAX_LISTING_BYTES = 8 * 1024 * 1024


class LMStudioProvider(CustomOpenAIProvider):
    """LM Studio, on the route of the integration feeding it (its relay group, or direct)."""

    provider_name = "lmstudio"
    integration_fed_only = True
    default_model = "default"
    # Local models: a model loaded on demand gets LM Studio's default context,
    # far below a cloud model's, and tool selection degrades with many tools.
    # The config's per-model windows (the loaded instance's, set when a model
    # is picked in the integration's Models tab) take precedence.
    context_window = 32_768
    max_output_tokens = 4_096
    max_tools = 64

    def __init__(self, api_key: str, *, provider_config_id: Any = None, **kwargs: Any) -> None:
        from app.services.lmstudio_llm_provider import PLACEHOLDER_ROOT, integration_http_client

        # The config the transport reads its integration through
        # (``router.provider_instance``); ``None`` refuses every request.
        self.base_url = f"{PLACEHOLDER_ROOT}/v1"
        self._http = integration_http_client(_as_uuid(provider_config_id), timeout=_TIMEOUT)
        # The key the SDK sends is replaced by the integration's token, if any.
        OpenAICompatibleProvider.__init__(self, api_key="lmstudio", http_client=self._http, **kwargs)

    def _auth_headers(self) -> dict[str, str]:
        # The transport sets the integration's token itself.
        return {}

    async def _native_models(self) -> list[dict]:
        """``GET /api/v1/models``, typed by the integration client's parser."""
        from app.integrations.providers.lmstudio import parse_models
        from app.services.lmstudio_llm_provider import PLACEHOLDER_ROOT

        async with self._http.stream("GET", f"{PLACEHOLDER_ROOT}/api/v1/models", headers={"Accept-Encoding": "identity"}) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_raw():
                total += len(chunk)
                if total > _MAX_LISTING_BYTES:
                    raise ValueError("LM Studio model listing over the size cap")
                chunks.append(chunk)
            if response.status_code >= 400:
                raise httpx.HTTPStatusError(
                    _listing_failure(response.status_code, b"".join(chunks)),
                    request=response.request,
                    response=response,
                )
        return parse_models(json.loads(b"".join(chunks)))

    async def _keys_of_type(self, model_type: str) -> list[str]:
        return sorted(m["key"] for m in await self._native_models() if m["type"] == model_type)

    async def _list_model_entries(self) -> list[dict]:
        """The OpenAI-compatible ``/models`` entries, through the routed client."""
        response = await self.client.models.list()
        return [{"id": m.id} for m in response.data]

    async def list_models(self) -> list[str]:
        """The chat models on the server (``type`` ``llm``), loaded or not."""
        return await self._keys_of_type("llm")

    async def list_embedding_models(self) -> list[str]:
        try:
            return await self._keys_of_type("embedding")
        except Exception:
            return []

    async def list_decision_models(self) -> list[str]:
        return await self._keys_of_type("decision")


def _listing_failure(status: int, body: bytes) -> str:
    """Why the listing failed: the transport's own refusal is said as it is, LM Studio's status alone."""
    from app.services.lmstudio_llm_provider import REFUSED_STATUS

    if status == REFUSED_STATUS:
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = None
        if isinstance(message, str):
            return message[:300]
    return f"LM Studio model listing answered HTTP {status}"


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
