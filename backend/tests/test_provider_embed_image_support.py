"""A provider is offered for embeddings or images only when its class serves them.

The router's capability lists name the providers it falls back to; each one
must implement the call rather than inherit the ``NotImplementedError`` of
``OpenAICompatibleProvider.embed`` or ``BaseLLMProvider.generate_image``.
"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai
import pytest

from app.llm import router
from app.llm.azure_openai_provider import AzureOpenAIProvider
from app.llm.base import BaseLLMProvider
from app.llm.litellm_provider import LiteLLMProvider
from app.llm.openai_compat import OpenAICompatibleProvider
from app.llm.openrouter_provider import OpenRouterProvider

_EMBEDDING_STUBS = (OpenAICompatibleProvider.embed, BaseLLMProvider.embed)


@pytest.mark.parametrize("name", sorted(set(router._EMBEDDING_PROVIDERS) | set(router.EMBEDDING_DEFAULTS)))
def test_every_embedding_provider_of_the_router_embeds(name):
    assert router.PROVIDER_MAP[name].embed not in _EMBEDDING_STUBS


@pytest.mark.parametrize("name", sorted(set(router._IMAGE_PROVIDERS) | set(router.IMAGE_DEFAULTS)))
def test_every_image_provider_of_the_router_draws(name):
    assert router.PROVIDER_MAP[name].generate_image is not BaseLLMProvider.generate_image


@pytest.mark.parametrize("cls", [LiteLLMProvider, OpenRouterProvider])
def test_the_gateways_offered_for_embeddings_in_settings_embed(cls):
    assert cls.embed not in _EMBEDDING_STUBS


def _azure() -> AzureOpenAIProvider:
    return AzureOpenAIProvider(api_key="https://res.openai.azure.com||key||2024-10-21")


class _Spy:
    def __init__(self, result):
        self.calls: list[dict] = []
        self._result = result

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _embedding(vector):
    return SimpleNamespace(data=[SimpleNamespace(embedding=vector)])


def test_azure_embeds_with_the_deployment_named():
    provider = _azure()
    spy = _Spy(_embedding([0.1, 0.2]))
    with patch.object(provider.client.embeddings, "create", new=spy):
        assert asyncio.run(provider.embed("hello", model="my-embedding")) == [0.1, 0.2]
    assert spy.calls == [{"input": "hello", "model": "my-embedding"}]


def test_azure_embeds_with_its_default_model_when_none_is_named():
    provider = _azure()
    spy = _Spy(_embedding([1.0]))
    with patch.object(provider.client.embeddings, "create", new=spy):
        asyncio.run(provider.embed("hello"))
    assert spy.calls == [{"input": "hello", "model": router.EMBEDDING_DEFAULTS["azure_openai"]}]


def _image(b64):
    return SimpleNamespace(data=[SimpleNamespace(b64_json=b64)])


def test_azure_draws_a_gpt_image_deployment_without_response_format():
    provider = _azure()
    spy = _Spy(_image(base64.b64encode(b"PNG").decode()))
    with patch.object(provider.client.images, "generate", new=spy):
        assert asyncio.run(provider.generate_image("a cat", model="gpt-image-1", size="1536x1024")) == b"PNG"
    assert spy.calls == [{"model": "gpt-image-1", "prompt": "a cat", "size": "1536x1024", "n": 1}]


def test_azure_asks_a_dall_e_deployment_for_base64():
    provider = _azure()
    spy = _Spy(_image(base64.b64encode(b"JPG").decode()))
    with patch.object(provider.client.images, "generate", new=spy):
        assert asyncio.run(provider.generate_image("a cat", model="dall-e-3")) == b"JPG"
    assert spy.calls == [
        {"model": "dall-e-3", "prompt": "a cat", "size": "1024x1024", "n": 1, "response_format": "b64_json"}
    ]


def test_azure_needs_an_image_deployment():
    with pytest.raises(ValueError, match="image deployment"):
        asyncio.run(_azure().generate_image("a cat"))


@pytest.mark.parametrize("answer", [SimpleNamespace(data=[]), _image(None), _image("")])
def test_azure_says_when_a_deployment_returns_no_image(answer):
    provider = _azure()
    with (
        patch.object(provider.client.images, "generate", new=_Spy(answer)),
        pytest.raises(RuntimeError, match="returned no image"),
    ):
        asyncio.run(provider.generate_image("a cat", model="gpt-image-1"))


def _listing(*ids):
    return SimpleNamespace(data=[SimpleNamespace(id=i) for i in ids])


def test_azure_offers_its_listed_embedding_and_image_models_by_the_catalog():
    provider = _azure()
    listing = _listing("gpt-4o", "text-embedding-3-large", "text-embedding-3-small", "gpt-image-1", "dall-e-3")
    with patch.object(provider.client.models, "list", new=_Spy(listing)):
        assert asyncio.run(provider.list_embedding_models()) == ["text-embedding-3-small", "text-embedding-3-large"]
        assert asyncio.run(provider.list_image_models()) == ["gpt-image-1", "dall-e-3"]


def test_azure_offers_no_model_when_its_listing_fails():
    provider = _azure()

    async def broken():
        raise openai.APIConnectionError(request=httpx.Request("GET", "https://res.openai.azure.com"))

    with patch.object(provider.client.models, "list", new=broken):
        assert asyncio.run(provider.list_embedding_models()) == []
        assert asyncio.run(provider.list_image_models()) == []


def test_azure_listing_lets_a_refused_key_through():
    provider = _azure()
    request = httpx.Request("GET", "https://res.openai.azure.com")

    async def refused():
        raise openai.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)

    with patch.object(provider.client.models, "list", new=refused), pytest.raises(openai.AuthenticationError):
        asyncio.run(provider.list_embedding_models())


def _litellm() -> LiteLLMProvider:
    return LiteLLMProvider(api_key="key||http://proxy:4000")


def _openrouter() -> OpenRouterProvider:
    return OpenRouterProvider(api_key="key")


@pytest.mark.parametrize("make", [_litellm, _openrouter], ids=["litellm", "openrouter"])
def test_a_gateway_embeds_with_the_model_chosen(make):
    provider = make()
    spy = _Spy(_embedding([0.5]))
    with patch.object(provider.client.embeddings, "create", new=spy):
        assert asyncio.run(provider.embed("hello", model="text-embedding-3-small")) == [0.5]
    assert spy.calls == [{"input": "hello", "model": "text-embedding-3-small"}]


@pytest.mark.parametrize("make", [_litellm, _openrouter], ids=["litellm", "openrouter"])
def test_a_gateway_has_no_default_embedding_model(make):
    with pytest.raises(ValueError, match="embedding model chosen in Settings"):
        asyncio.run(make().embed("hello"))


def _get_answering(payload):
    calls: list[str] = []

    async def get(path, **_kwargs):
        calls.append(path)
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(200, json=payload, request=httpx.Request("GET", f"https://host/{path}"))

    return get, calls


def test_litellm_offers_the_aliases_its_model_info_declares_as_embeddings():
    provider = _litellm()
    get, calls = _get_answering(
        {
            "data": [
                {"model_name": "embedder", "model_info": {"mode": "embedding"}},
                {"model_name": "chatty", "model_info": {"mode": "chat"}},
            ]
        }
    )
    with patch.object(provider.client, "get", new=get):
        assert asyncio.run(provider.list_embedding_models()) == ["embedder"]
    assert calls == ["model/info"]


def test_openrouter_offers_its_embedding_catalog():
    provider = _openrouter()
    get, calls = _get_answering(
        {
            "data": [
                {"id": "openai/text-embedding-3-small"},
                {"id": "mistralai/mistral-embed"},
                {"id": ""},
                {"id": "x" * 201},
                {"id": 42},
                "not-a-model",
            ]
        }
    )
    with patch.object(provider.client, "get", new=get):
        assert asyncio.run(provider.list_embedding_models()) == [
            "mistralai/mistral-embed",
            "openai/text-embedding-3-small",
        ]
    assert calls == ["embeddings/models"]


@pytest.mark.parametrize("payload", [{"data": "nope"}, ["not", "an", "object"], {}])
def test_openrouter_offers_nothing_from_a_malformed_catalog(payload):
    provider = _openrouter()
    get, _calls = _get_answering(payload)
    with patch.object(provider.client, "get", new=get):
        assert asyncio.run(provider.list_embedding_models()) == []


def test_openrouter_offers_nothing_when_its_catalog_is_unreachable():
    provider = _openrouter()
    get, _calls = _get_answering(openai.APIConnectionError(request=httpx.Request("GET", "https://openrouter.ai")))
    with patch.object(provider.client, "get", new=get):
        assert asyncio.run(provider.list_embedding_models()) == []


def test_openrouter_catalog_lets_a_refused_key_through():
    provider = _openrouter()
    request = httpx.Request("GET", "https://openrouter.ai")
    get, _calls = _get_answering(
        openai.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    )
    with patch.object(provider.client, "get", new=get), pytest.raises(openai.AuthenticationError):
        asyncio.run(provider.list_embedding_models())


def test_embedding_models_are_recognised_by_the_catalog():
    assert router.is_embedding_model("text-embedding-3-large")
    assert router.is_embedding_model("azure/text-embedding-3-small")
    assert not router.is_embedding_model("gpt-4o")
    assert not router.is_embedding_model("my-embedding-deployment")
    assert not router.is_embedding_model(None)
