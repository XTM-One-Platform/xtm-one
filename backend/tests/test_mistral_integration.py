"""Tests for the Mistral AI direct-query integration provider.

The provider exposes Mistral as a queryable/evaluable *tool target* (distinct
from the platform LLM provider system). These tests pin the request payloads
each tool builds and the way responses are formatted, including the connector
``messages`` array form used by the web search tool. They monkeypatch the
module-level ``_request`` helper.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import mistral
from app.integrations.providers.mistral import build_tools
from app.tools.base import CallableToolExecutor

_TOKEN = "mistral-test"


def _handlers(token: str = _TOKEN) -> dict:
    executor = CallableToolExecutor()
    build_tools(token, executor)
    return executor._handlers


def _patch(monkeypatch, *, data: dict | None = None, err: str | None = None) -> dict:
    captured: dict = {}

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        captured.update(
            api_key=api_key, method=method, path=path, action=action, json=json
        )
        return data, err

    monkeypatch.setattr(mistral, "_request", _fake)
    return captured


def _no_call(monkeypatch) -> None:
    async def _must_not(*args, **kwargs):
        raise AssertionError("_request must not be called")

    monkeypatch.setattr(mistral, "_request", _must_not)


def _chat(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "model": "mistral-large-latest",
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }


@pytest.mark.asyncio
async def test_query_builds_payload_and_formats(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("Bonjour"))
    out = await _handlers()["query_mistral"](
        prompt="Hi", system_prompt="be brief", temperature=0.3, max_tokens=200
    )
    assert "Bonjour" in out
    assert "model=mistral-large-latest" in out
    assert captured["path"] == "/chat/completions"
    body = captured["json"]
    assert body["model"] == "mistral-large-latest"
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "Hi"}
    assert body["max_tokens"] == 200
    assert body["temperature"] == 0.3


@pytest.mark.asyncio
async def test_query_requires_prompt(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely (the tool-call-missing-field case): the handler defaults
    # 'prompt' so validation runs instead of raising a TypeError.
    assert "required" in (await _handlers()["query_mistral"]()).lower()
    # A blank string is rejected the same way.
    assert "required" in (await _handlers()["query_mistral"](prompt="   ")).lower()


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits():
    out = await _handlers(token="")["query_mistral"](prompt="hi")
    assert "api key is missing" in out.lower()


@pytest.mark.asyncio
async def test_list_models_shows_capability_tags(monkeypatch):
    _patch(
        monkeypatch,
        data={
            "data": [
                {
                    "id": "mistral-large-latest",
                    "capabilities": {"completion_chat": True, "function_calling": True},
                },
                {"id": "pixtral-12b", "capabilities": {"vision": True}},
            ]
        },
    )
    out = await _handlers()["list_mistral_models"]()
    assert "mistral-large-latest" in out
    assert "tools" in out
    assert "pixtral-12b" in out
    assert "vision" in out


@pytest.mark.asyncio
async def test_evaluate_uses_judge_system_and_zero_temperature(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("SCORE: 6"))
    out = await _handlers()["evaluate_with_mistral"](
        content="answer", criteria="accuracy", scale="1-5", reference="truth"
    )
    assert "SCORE: 6" in out
    body = captured["json"]
    assert body["temperature"] == 0
    assert "1-5" in body["messages"][0]["content"]
    assert "truth" in body["messages"][1]["content"]
    assert "answer" in body["messages"][1]["content"]


@pytest.mark.asyncio
async def test_evaluate_requires_inputs(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely: defaults let the validation return the friendly error
    # rather than raising a TypeError on the missing positional arguments.
    assert "required" in (await _handlers()["evaluate_with_mistral"]()).lower()
    # A blank field is rejected the same way.
    out = await _handlers()["evaluate_with_mistral"](content="x", criteria="")
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_compare_models_runs_all(monkeypatch):
    calls: list[str] = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        calls.append(json["model"])
        return _chat(f"answer from {json['model']}"), None

    monkeypatch.setattr(mistral, "_request", _fake)
    out = await _handlers()["compare_mistral_models"](prompt="hi", models=["a", "b"])
    assert "a" in out
    assert "b" in out
    assert set(calls) == {"a", "b"}


@pytest.mark.asyncio
async def test_compare_requires_models(monkeypatch):
    _no_call(monkeypatch)
    # 'models' omitted entirely: defaults to None and coerces to [], so the
    # handler returns the friendly error instead of raising a TypeError.
    assert "at least one model" in (
        await _handlers()["compare_mistral_models"](prompt="hi")
    ).lower()
    # An explicit empty list is rejected the same way.
    out = await _handlers()["compare_mistral_models"](prompt="hi", models=[])
    assert "at least one model" in out.lower()


@pytest.mark.asyncio
async def test_web_search_parses_connector_messages_array(monkeypatch):
    data = {
        "model": "mistral-large-latest",
        "choices": [
            {
                "finish_reason": "stop",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Acme raised funding. "},
                            {
                                "type": "tool_reference",
                                "url": "https://news.example/acme",
                                "title": "Acme News",
                            },
                            {"type": "text", "text": "More details."},
                        ],
                    }
                ],
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    captured = _patch(monkeypatch, data=data)
    out = await _handlers()["query_mistral_web_search"](query="Acme news?", max_tokens=400)
    assert "Acme raised funding." in out
    assert "More details." in out
    assert "https://news.example/acme" in out
    body = captured["json"]
    assert body["tools"] == [{"type": "web_search"}]
    assert body["max_tokens"] == 400
