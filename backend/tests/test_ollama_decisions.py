"""Ollama serves decision models (CLMs) on its System One endpoint.

Clef answers typed questions over a state instead of writing text. Ollama
reports it in each model's ``capabilities`` and serves it on ``/v1/systemone``,
the wire format ``app.llm.decisions`` already speaks for OpenRouter.
"""

import json

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from app.llm.base import BaseLLMProvider
from app.llm.decisions import DECISIONS_SYSTEM_ONE
from app.llm.ollama_provider import OllamaProvider


def _provider(handler) -> tuple[OllamaProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider = OllamaProvider(api_key="http://ollama.test:11434")
    provider.client = AsyncOpenAI(
        api_key="ollama",
        base_url=provider.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_record)),
        max_retries=0,
    )
    return provider, seen


@pytest.mark.asyncio
async def test_a_choice_goes_to_the_system_one_endpoint():
    answer = {
        "model": "clef-flash",
        "answers": {"label": {"type": "choice", "choice": "b", "probabilities": {"a": 0.1, "b": 0.9}, "confidence": 0.8}},
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }
    provider, seen = _provider(lambda _r: httpx.Response(200, json=answer))

    decision = await provider.decide_choice(
        model="clef-flash", state="page", instructions="Pick", choices={"a": "A", "b": "B"}
    )

    assert provider.decisions_api == DECISIONS_SYSTEM_ONE
    assert str(seen[0].url) == "http://ollama.test:11434/v1/systemone"
    assert json.loads(seen[0].content)["model"] == "clef-flash"
    assert (decision.choice, decision.confidence) == ("b", 0.8)


@pytest.mark.asyncio
async def test_list_decision_models_keeps_decision_capable_models():
    tags = {
        "models": [
            {"name": "clef-flash:latest", "capabilities": ["decision", "vision"]},
            {"name": "qwen3.6:latest", "capabilities": ["completion", "tools"]},
            {"name": "legacy:latest"},
        ]
    }
    provider, seen = _provider(lambda _r: httpx.Response(200, json=tags))

    assert await provider.list_decision_models() == ["clef-flash:latest"]
    assert str(seen[0].url) == "http://ollama.test:11434/api/tags"


@pytest.mark.asyncio
async def test_list_decision_models_raises_when_ollama_is_unreachable():
    # Not an empty listing: a Classify node must not read a down Ollama as one
    # serving no decision model (the settings picker lists nothing for it).
    def _down(_r):
        raise httpx.ConnectError("refused")

    provider, _ = _provider(_down)
    with pytest.raises(openai.APIConnectionError):
        await provider.list_decision_models()


@pytest.mark.asyncio
async def test_a_provider_lists_no_decision_models_by_default():
    class _Plain(BaseLLMProvider):
        provider_name = "plain"

        async def chat(self, *a, **k):  # pragma: no cover - not called
            raise NotImplementedError

        async def embed(self, *a, **k):  # pragma: no cover - not called
            raise NotImplementedError

        async def list_models(self):
            return []

    assert await _Plain.__new__(_Plain).list_decision_models() == []


@pytest.mark.asyncio
async def test_decision_context_window_is_the_num_ctx_ollama_runs_the_model_with():
    show = {"parameters": "num_ctx                        2048\ntemperature                    0", "capabilities": ["decision"]}
    provider, seen = _provider(lambda _r: httpx.Response(200, json=show))

    assert await provider.decision_context_window("tev1:4b") == 2048
    assert str(seen[0].url) == "http://ollama.test:11434/api/show"
    assert json.loads(seen[0].content) == {"model": "tev1:4b"}


@pytest.mark.asyncio
async def test_decision_context_window_prefers_the_administrators_window():
    provider, seen = _provider(lambda _r: httpx.Response(200, json={"parameters": "num_ctx 2048"}))
    provider.context_windows = {"tev1:4b": 4096}

    assert await provider.decision_context_window("tev1:4b") == 4096
    assert seen == []


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json={"parameters": "temperature 0"}), httpx.Response(200, json={}), httpx.Response(404, json={})],
)
@pytest.mark.asyncio
async def test_decision_context_window_is_unknown_without_num_ctx(response):
    provider, _ = _provider(lambda _r: response)

    assert await provider.decision_context_window("clef-flash") is None
