"""Tests for the OpenAI direct-query integration provider.

The provider exposes OpenAI as a queryable/evaluable *tool target* (distinct
from the platform LLM provider system). These tests pin the request payloads
each tool builds and the way responses are formatted, by monkeypatching the
module-level ``_request`` helper.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import openai as openai_integration
from app.integrations.providers.openai import build_tools
from app.tools.base import CallableToolExecutor

_TOKEN = "sk-test"


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

    monkeypatch.setattr(openai_integration, "_request", _fake)
    return captured


def _no_call(monkeypatch) -> None:
    async def _must_not(*args, **kwargs):
        raise AssertionError("_request must not be called")

    monkeypatch.setattr(openai_integration, "_request", _must_not)


def _chat(text: str, model: str = "gpt-4o") -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "model": model,
        "usage": {"prompt_tokens": 4, "completion_tokens": 6},
    }


@pytest.mark.asyncio
async def test_query_uses_max_tokens_and_temperature_for_standard_model(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("Hello back"))
    out = await _handlers()["query_openai"](
        prompt="Hi", system_prompt="be nice", temperature=0.5, max_tokens=300
    )
    assert "Hello back" in out
    assert "model=gpt-4o" in out
    body = captured["json"]
    assert captured["path"] == "/chat/completions"
    assert body["model"] == "gpt-4o"
    assert body["messages"][0] == {"role": "system", "content": "be nice"}
    assert body["messages"][1] == {"role": "user", "content": "Hi"}
    assert body["max_tokens"] == 300
    assert body["temperature"] == 0.5
    assert "max_completion_tokens" not in body


@pytest.mark.asyncio
async def test_query_reasoning_model_uses_completion_tokens_and_drops_temperature(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("ok", model="gpt-5"))
    await _handlers()["query_openai"](prompt="Hi", model="gpt-5", temperature=0.9)
    body = captured["json"]
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    assert "temperature" not in body


@pytest.mark.asyncio
async def test_query_clamps_max_tokens_to_cap(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("ok"))
    await _handlers()["query_openai"](prompt="hi", max_tokens=999_999)
    assert captured["json"]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_chatgpt_model_keeps_temperature_with_completion_tokens(monkeypatch):
    # chatgpt-4o-latest needs max_completion_tokens but still accepts a custom
    # temperature, so the two concerns must stay decoupled.
    captured = _patch(monkeypatch, data=_chat("ok", model="chatgpt-4o-latest"))
    await _handlers()["query_openai"](
        prompt="Hi", model="chatgpt-4o-latest", temperature=0.5
    )
    body = captured["json"]
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    assert body["temperature"] == 0.5


@pytest.mark.asyncio
async def test_query_requires_prompt(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely (the tool-call-missing-field case): the handler defaults
    # 'prompt' so validation runs instead of raising a TypeError.
    assert "required" in (await _handlers()["query_openai"]()).lower()
    # A blank string is rejected the same way.
    assert "required" in (await _handlers()["query_openai"](prompt="  ")).lower()


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits():
    out = await _handlers(token="")["query_openai"](prompt="hi")
    assert "api key is missing" in out.lower()


@pytest.mark.asyncio
async def test_list_models_sorts_desc(monkeypatch):
    _patch(monkeypatch, data={"data": [{"id": "gpt-3.5"}, {"id": "gpt-4o"}]})
    out = await _handlers()["list_openai_models"]()
    assert out.index("gpt-4o") < out.index("gpt-3.5")


@pytest.mark.asyncio
async def test_evaluate_uses_judge_system_and_zero_temperature(monkeypatch):
    captured = _patch(monkeypatch, data=_chat("SCORE: 7"))
    out = await _handlers()["evaluate_with_openai"](
        content="answer", criteria="accuracy", scale="1-5", reference="truth"
    )
    assert "SCORE: 7" in out
    body = captured["json"]
    assert body["temperature"] == 0.0
    assert "1-5" in body["messages"][0]["content"]
    assert "truth" in body["messages"][1]["content"]
    assert "answer" in body["messages"][1]["content"]


@pytest.mark.asyncio
async def test_evaluate_requires_inputs(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely: defaults let the validation return the friendly error
    # rather than raising a TypeError on the missing positional arguments.
    assert "required" in (await _handlers()["evaluate_with_openai"]()).lower()
    # A blank field is rejected the same way.
    out = await _handlers()["evaluate_with_openai"](content="x", criteria="")
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_compare_models_runs_all(monkeypatch):
    calls: list[str] = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        calls.append(json["model"])
        return _chat(f"answer from {json['model']}", model=json["model"]), None

    monkeypatch.setattr(openai_integration, "_request", _fake)
    out = await _handlers()["compare_openai_models"](prompt="hi", models=["a", "b"])
    assert "a" in out
    assert "b" in out
    assert set(calls) == {"a", "b"}


@pytest.mark.asyncio
async def test_compare_requires_models(monkeypatch):
    _no_call(monkeypatch)
    # 'models' omitted entirely: defaults to None and coerces to [], so the
    # handler returns the friendly error instead of raising a TypeError.
    assert "at least one model" in (
        await _handlers()["compare_openai_models"](prompt="hi")
    ).lower()
    # An explicit empty list is rejected the same way.
    out = await _handlers()["compare_openai_models"](prompt="hi", models=[])
    assert "at least one model" in out.lower()


@pytest.mark.asyncio
async def test_compare_uses_constant_action_for_logging(monkeypatch):
    # safe_request logs the action string, so the user-controlled model id must
    # not be embedded in it (log-forging guard). The model id is still surfaced
    # per-model in the returned block.
    actions: list[str] = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        actions.append(action)
        return _chat("ok", model=json["model"]), None

    monkeypatch.setattr(openai_integration, "_request", _fake)
    out = await _handlers()["compare_openai_models"](
        prompt="hi", models=["gpt-4o", "evil\nINJECTED LOG LINE"]
    )
    assert actions == ["query the model", "query the model"]
    assert "gpt-4o" in out  # model id stays attached to its output block


@pytest.mark.asyncio
async def test_web_search_parses_output_and_citations(monkeypatch):
    data = {
        "model": "gpt-4o",
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Acme launched a product.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://news.example/acme",
                                "title": "Acme News",
                            }
                        ],
                    }
                ],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    captured = _patch(monkeypatch, data=data)
    out = await _handlers()["query_openai_web_search"](query="Acme news?", max_tokens=500)
    assert "Acme launched a product." in out
    assert "https://news.example/acme" in out
    body = captured["json"]
    assert captured["path"] == "/responses"
    assert body["tools"] == [{"type": "web_search"}]
    assert body["max_output_tokens"] == 500
    assert body["input"] == "Acme news?"


@pytest.mark.asyncio
async def test_web_search_floors_max_output_tokens(monkeypatch):
    # The Responses API rejects max_output_tokens below 16, so the tool floors
    # any smaller request up to that minimum rather than sending an invalid value.
    captured = _patch(monkeypatch, data={"model": "gpt-4o", "output": []})
    await _handlers()["query_openai_web_search"](query="hi", max_tokens=1)
    assert captured["json"]["max_output_tokens"] == 16
