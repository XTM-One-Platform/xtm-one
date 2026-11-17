"""Tests for the Anthropic direct-query integration provider.

The provider exposes Claude as a queryable/evaluable *tool target* (distinct
from the platform LLM provider system). These tests pin the request payloads
each tool builds and the way responses are formatted, by monkeypatching the
module-level ``_request`` helper (mirrors ``test_ses_integration``).
"""

from __future__ import annotations

import pytest

from app.integrations.providers import anthropic
from app.integrations.providers.anthropic import build_tools
from app.tools.base import CallableToolExecutor

_TOKEN = "sk-ant-test"


def _handlers(token: str = _TOKEN) -> dict:
    executor = CallableToolExecutor()
    build_tools(token, executor)
    return executor._handlers


def _patch(monkeypatch, *, data: dict | None = None, err: str | None = None) -> dict:
    """Patch ``anthropic._request`` to capture the call and return a canned result."""
    captured: dict = {}

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        captured.update(
            api_key=api_key,
            method=method,
            path=path,
            action=action,
            json=json,
            params=params,
        )
        return data, err

    monkeypatch.setattr(anthropic, "_request", _fake)
    return captured


def _no_call(monkeypatch) -> None:
    async def _must_not(*args, **kwargs):
        raise AssertionError("_request must not be called")

    monkeypatch.setattr(anthropic, "_request", _must_not)


@pytest.mark.asyncio
async def test_query_builds_payload_and_formats(monkeypatch):
    captured = _patch(
        monkeypatch,
        data={
            "content": [{"type": "text", "text": "Hi there"}],
            "model": "claude-x",
            "usage": {"input_tokens": 3, "output_tokens": 5},
            "stop_reason": "end_turn",
        },
    )
    out = await _handlers()["query_anthropic"](
        prompt="Hello", system_prompt="be brief", temperature=0.3, max_tokens=200
    )
    assert "Hi there" in out
    assert "model=claude-x" in out
    assert captured["path"] == "/messages"
    body = captured["json"]
    assert body["messages"][0]["content"] == "Hello"
    assert body["system"] == "be brief"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 200


@pytest.mark.asyncio
async def test_query_requires_prompt(monkeypatch):
    _no_call(monkeypatch)
    out = await _handlers()["query_anthropic"](prompt="   ")
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits():
    # The real _request guards a blank key before any HTTP call.
    out = await _handlers(token="")["query_anthropic"](prompt="hi")
    assert "api key is missing" in out.lower()


@pytest.mark.asyncio
async def test_query_max_tokens_is_clamped(monkeypatch):
    captured = _patch(monkeypatch, data={"content": [{"type": "text", "text": "ok"}]})
    await _handlers()["query_anthropic"](prompt="hi", max_tokens=999_999)
    assert captured["json"]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_list_models_formats(monkeypatch):
    _patch(
        monkeypatch,
        data={
            "data": [
                {"id": "claude-a", "display_name": "Claude A"},
                {"id": "claude-b"},
            ]
        },
    )
    out = await _handlers()["list_anthropic_models"]()
    assert "claude-a" in out
    assert "Claude A" in out
    assert "claude-b" in out


@pytest.mark.asyncio
async def test_evaluate_uses_judge_system_and_zero_temperature(monkeypatch):
    captured = _patch(monkeypatch, data={"content": [{"type": "text", "text": "SCORE: 8"}]})
    out = await _handlers()["evaluate_with_anthropic"](
        content="some answer", criteria="accuracy", scale="1-5", reference="truth"
    )
    assert "SCORE: 8" in out
    body = captured["json"]
    assert body["temperature"] == 0
    assert "1-5" in body["system"]
    user = body["messages"][0]["content"]
    assert "truth" in user
    assert "some answer" in user


@pytest.mark.asyncio
async def test_evaluate_requires_inputs(monkeypatch):
    _no_call(monkeypatch)
    out = await _handlers()["evaluate_with_anthropic"](content="", criteria="x")
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_compare_models_runs_all(monkeypatch):
    calls: list[str] = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        calls.append(json["model"])
        return {"content": [{"type": "text", "text": f"answer from {json['model']}"}]}, None

    monkeypatch.setattr(anthropic, "_request", _fake)
    out = await _handlers()["compare_anthropic_models"](prompt="hi", models=["m1", "m2"])
    assert "m1" in out
    assert "m2" in out
    assert set(calls) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_compare_reuses_single_client(monkeypatch):
    # The concurrent per-model calls must share one AsyncClient (connection
    # pooling) rather than each spinning up a fresh client/socket.
    clients: list = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        clients.append(client)
        return {"content": [{"type": "text", "text": "ok"}]}, None

    monkeypatch.setattr(anthropic, "_request", _fake)
    await _handlers()["compare_anthropic_models"](prompt="hi", models=["m1", "m2", "m3"])
    assert len(clients) == 3
    assert all(c is not None for c in clients)
    assert len({id(c) for c in clients}) == 1


@pytest.mark.asyncio
async def test_compare_requires_models(monkeypatch):
    _no_call(monkeypatch)
    out = await _handlers()["compare_anthropic_models"](prompt="hi", models=[])
    assert "at least one model" in out.lower()


@pytest.mark.asyncio
async def test_compare_handles_omitted_models_arg(monkeypatch):
    # A malformed tool call that omits the `models` argument entirely must hit
    # the friendly validation path, not raise a TypeError (handlers default
    # every schema-"required" param).
    _no_call(monkeypatch)
    out = await _handlers()["compare_anthropic_models"](prompt="hi")
    assert "at least one model" in out.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "query_anthropic",
        "evaluate_with_anthropic",
        "compare_anthropic_models",
        "query_anthropic_web_search",
    ],
)
async def test_handlers_validate_when_called_with_no_args(monkeypatch, tool):
    # Calling a handler with no arguments at all (the worst-case malformed tool
    # call) must return a friendly "... is required" message rather than raising
    # a TypeError that the executor would surface as a raw exception string.
    _no_call(monkeypatch)
    out = await _handlers()[tool]()
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_web_search_extracts_sources_and_sets_tool(monkeypatch):
    data = {
        "content": [
            {
                "type": "text",
                "text": "Acme is a company. ",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://a.com",
                        "title": "A",
                    }
                ],
            },
            {
                "type": "web_search_tool_result",
                "content": [
                    {"type": "web_search_result", "url": "https://b.com", "title": "B"}
                ],
            },
            {"type": "text", "text": "More info."},
        ],
        "model": "claude-x",
    }
    captured = _patch(monkeypatch, data=data)
    out = await _handlers()["query_anthropic_web_search"](
        query="What about Acme?", max_searches=3
    )
    assert "Acme is a company." in out
    assert "More info." in out
    assert "https://a.com" in out
    assert "https://b.com" in out
    tool = captured["json"]["tools"][0]
    assert tool["type"] == anthropic._WEB_SEARCH_TOOL
    assert tool["max_uses"] == 3
