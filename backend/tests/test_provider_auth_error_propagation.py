"""Tests that provider list_* methods propagate authentication errors.

When an invalid API key is provided, the provider's list_models(),
list_embedding_models(), and list_image_models() methods must re-raise
authentication errors rather than swallowing them and returning hardcoded
fallback lists.  This ensures that the ``POST /settings/llm-providers/validate``
endpoint correctly rejects invalid credentials instead of falsely reporting
"Provider validated and models discovered successfully."
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from app.llm.azure_openai_provider import AzureOpenAIProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.litellm_provider import LiteLLMProvider
from app.llm.mistral_provider import MistralProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.xai_provider import XAIProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_auth_error() -> openai.AuthenticationError:
    """Create a minimal openai.AuthenticationError (HTTP 401)."""
    fake_response = httpx.Response(
        401, request=httpx.Request("GET", "https://api.openai.com/v1/models")
    )
    return openai.AuthenticationError(
        "Incorrect API key provided",
        response=fake_response,
        body={"error": {"message": "Incorrect API key provided", "type": "authentication_error"}},
    )


def _make_openai_permission_error() -> openai.PermissionDeniedError:
    """Create a minimal openai.PermissionDeniedError (HTTP 403)."""
    fake_response = httpx.Response(
        403, request=httpx.Request("GET", "https://api.openai.com/v1/models")
    )
    return openai.PermissionDeniedError(
        "Access denied",
        response=fake_response,
        body={"error": {"message": "Access denied", "type": "access_denied"}},
    )


def _make_genai_client_error(code: int = 401) -> genai_errors.ClientError:
    """Create a minimal google.genai.errors.ClientError for the given status code."""
    status = "UNAUTHENTICATED" if code == 401 else "PERMISSION_DENIED"
    return genai_errors.ClientError(
        code,
        {"error": {"message": "API key not valid.", "status": status, "code": code}},
    )


def _make_fake_models_response(model_ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(id=mid) for mid in model_ids])


# ---------------------------------------------------------------------------
# OpenAIProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_list_models_reraises_authentication_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_openai_list_models_reraises_permission_denied_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_permission_error()
    ), pytest.raises(openai.PermissionDeniedError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_openai_list_models_returns_fallback_on_generic_error():
    """Non-auth errors (e.g. network error) must still return the fallback list."""
    provider = OpenAIProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_models()
    assert models  # fallback is non-empty
    assert "gpt-4o" in models


# ---------------------------------------------------------------------------
# OpenAIProvider — list_embedding_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_list_embedding_models_reraises_authentication_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_embedding_models()


@pytest.mark.asyncio
async def test_openai_list_embedding_models_reraises_permission_denied_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_permission_error()
    ), pytest.raises(openai.PermissionDeniedError):
        await provider.list_embedding_models()


@pytest.mark.asyncio
async def test_openai_list_embedding_models_returns_fallback_on_generic_error():
    provider = OpenAIProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_embedding_models()
    assert models
    assert "text-embedding-3-large" in models


# ---------------------------------------------------------------------------
# OpenAIProvider — list_image_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_list_image_models_reraises_authentication_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_image_models()


@pytest.mark.asyncio
async def test_openai_list_image_models_reraises_permission_denied_error():
    provider = OpenAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_permission_error()
    ), pytest.raises(openai.PermissionDeniedError):
        await provider.list_image_models()


@pytest.mark.asyncio
async def test_openai_list_image_models_returns_fallback_on_generic_error():
    provider = OpenAIProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_image_models()
    assert models
    assert "gpt-image-1" in models


# ---------------------------------------------------------------------------
# OpenRouterProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_list_models_reraises_authentication_error():
    provider = OpenRouterProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_openrouter_list_models_returns_fallback_on_generic_error():
    provider = OpenRouterProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_models()
    assert models


# ---------------------------------------------------------------------------
# MistralProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mistral_list_models_reraises_authentication_error():
    provider = MistralProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_mistral_list_models_returns_fallback_on_generic_error():
    provider = MistralProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_models()
    assert models
    assert "mistral-large-latest" in models
    assert "open-mistral-nemo" not in models


@pytest.mark.asyncio
async def test_mistral_list_models_filters_non_chat_models():
    provider = MistralProvider(api_key="test-key")
    fake = _make_fake_models_response(
        [
            "mistral-large-latest",
            "magistral-medium-latest",
            "mistral-embed",
            "codestral-embed",
            "mistral-moderation-2603",
            "mistral-ocr-latest",
            "voxtral-mini-transcribe-realtime-2602",
            "voxtral-mini-tts-2603",
        ]
    )
    with patch.object(provider.client.models, "list", new_callable=AsyncMock, return_value=fake):
        models = await provider.list_models()
    assert models == ["mistral-large-latest", "magistral-medium-latest"]


# ---------------------------------------------------------------------------
# OpenAIProvider — list_models filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_list_models_offers_only_chat_completions_models():
    """Responses-only models (pro, codex, deep research) 404 on chat completions."""
    provider = OpenAIProvider(api_key="test-key")
    fake = _make_fake_models_response(
        [
            "gpt-5.2",
            "gpt-5.2-pro",
            "o3",
            "o3-pro",
            "o1",
            "gpt-5-codex",
            "o4-mini-deep-research",
            "gpt-realtime",
            "text-embedding-3-large",
        ]
    )
    with patch.object(provider.client.models, "list", new_callable=AsyncMock, return_value=fake):
        models = await provider.list_models()
    assert models == ["o3", "o1", "gpt-5.2"]


# ---------------------------------------------------------------------------
# XAIProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xai_list_models_reraises_authentication_error():
    provider = XAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_xai_list_models_reraises_permission_denied_error():
    provider = XAIProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_permission_error()
    ), pytest.raises(openai.PermissionDeniedError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_xai_list_models_returns_fallback_on_generic_error():
    provider = XAIProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_models()
    assert models
    assert "grok-4.6" in models


@pytest.mark.asyncio
async def test_xai_list_models_filters_non_chat_models():
    provider = XAIProvider(api_key="test-key")
    fake = _make_fake_models_response(
        [
            "grok-4.6",
            "grok-build-0.1",
            "grok-imagine-2",
            "grok-voice-1",
            "grok-tts-1",
            "grok-stt-1",
        ]
    )
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, return_value=fake
    ):
        models = await provider.list_models()
    assert "grok-4.6" in models
    assert "grok-build-0.1" in models
    assert all(
        not any(kw in m for kw in ("imagine", "voice", "tts", "stt")) for m in models
    )


# ---------------------------------------------------------------------------
# AzureOpenAIProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_azure_list_models_reraises_authentication_error():
    provider = AzureOpenAIProvider(api_key="https://example.openai.azure.com||invalid-key||2024-10-21")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_azure_list_models_returns_fallback_on_generic_error():
    provider = AzureOpenAIProvider(api_key="https://example.openai.azure.com||test-key||2024-10-21")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("network error")
    ):
        models = await provider.list_models()
    assert models
    assert "gpt-4o" in models


# ---------------------------------------------------------------------------
# LiteLLMProvider — list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_litellm_list_models_reraises_authentication_error():
    provider = LiteLLMProvider(api_key="invalid-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=_make_openai_auth_error()
    ), pytest.raises(openai.AuthenticationError):
        await provider.list_models()


@pytest.mark.asyncio
async def test_litellm_list_models_returns_fallback_on_generic_error():
    provider = LiteLLMProvider(api_key="test-key")
    with patch.object(
        provider.client.models, "list", new_callable=AsyncMock, side_effect=Exception("proxy not running")
    ):
        models = await provider.list_models()
    assert models
    assert "gpt-4o" in models


# ---------------------------------------------------------------------------
# GeminiProvider — list_embedding_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_list_embedding_models_reraises_401_client_error():
    provider = GeminiProvider(api_key="invalid-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=_make_genai_client_error(401)
    ), pytest.raises(genai_errors.ClientError) as exc_info:
        await provider.list_embedding_models()
    assert exc_info.value.code == 401


@pytest.mark.asyncio
async def test_gemini_list_embedding_models_reraises_403_client_error():
    provider = GeminiProvider(api_key="invalid-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=_make_genai_client_error(403)
    ), pytest.raises(genai_errors.ClientError) as exc_info:
        await provider.list_embedding_models()
    assert exc_info.value.code == 403


@pytest.mark.asyncio
async def test_gemini_list_embedding_models_returns_fallback_on_generic_error():
    provider = GeminiProvider(api_key="test-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=Exception("network error")
    ):
        models = await provider.list_embedding_models()
    assert models
    assert "text-embedding-004" in models


@pytest.mark.asyncio
async def test_gemini_list_embedding_models_returns_fallback_on_non_auth_client_error():
    """A 429 rate-limit ClientError should use the fallback, not propagate."""
    provider = GeminiProvider(api_key="test-key")
    rate_limit_error = genai_errors.ClientError(
        429,
        {"error": {"message": "Too many requests", "status": "RESOURCE_EXHAUSTED", "code": 429}},
    )
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=rate_limit_error
    ):
        models = await provider.list_embedding_models()
    assert models
    assert "text-embedding-004" in models


# ---------------------------------------------------------------------------
# GeminiProvider — list_image_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_list_image_models_reraises_401_client_error():
    provider = GeminiProvider(api_key="invalid-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=_make_genai_client_error(401)
    ), pytest.raises(genai_errors.ClientError) as exc_info:
        await provider.list_image_models()
    assert exc_info.value.code == 401


@pytest.mark.asyncio
async def test_gemini_list_image_models_reraises_403_client_error():
    provider = GeminiProvider(api_key="invalid-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=_make_genai_client_error(403)
    ), pytest.raises(genai_errors.ClientError) as exc_info:
        await provider.list_image_models()
    assert exc_info.value.code == 403


@pytest.mark.asyncio
async def test_gemini_list_image_models_returns_fallback_on_generic_error():
    provider = GeminiProvider(api_key="test-key")
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=Exception("network error")
    ):
        models = await provider.list_image_models()
    assert models
    assert "imagen-3.0-generate-002" in models


@pytest.mark.asyncio
async def test_gemini_list_image_models_returns_fallback_on_non_auth_client_error():
    """A 429 rate-limit ClientError should use the fallback, not propagate."""
    provider = GeminiProvider(api_key="test-key")
    rate_limit_error = genai_errors.ClientError(
        429,
        {"error": {"message": "Too many requests", "status": "RESOURCE_EXHAUSTED", "code": 429}},
    )
    with patch.object(
        provider.client.aio.models, "list", new_callable=AsyncMock,
        side_effect=rate_limit_error
    ):
        models = await provider.list_image_models()
    assert models
    assert "imagen-3.0-generate-002" in models
