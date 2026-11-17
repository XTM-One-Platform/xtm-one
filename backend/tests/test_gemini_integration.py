"""Tests for the Google Gemini direct-query integration provider.

The provider exposes Gemini as a queryable/evaluable *tool target* (distinct
from the platform LLM provider system). These tests pin the request payloads
each tool builds and the way responses are formatted, by monkeypatching the
module-level ``_request`` helper.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import gemini
from app.integrations.providers.gemini import build_tools
from app.tools.base import CallableToolExecutor

_TOKEN = "AIza-test"


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

    monkeypatch.setattr(gemini, "_request", _fake)
    return captured


def _no_call(monkeypatch) -> None:
    async def _must_not(*args, **kwargs):
        raise AssertionError("_request must not be called")

    monkeypatch.setattr(gemini, "_request", _must_not)


def _gen(text: str) -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7},
    }


@pytest.mark.asyncio
async def test_query_builds_payload_and_formats(monkeypatch):
    captured = _patch(monkeypatch, data=_gen("Hello from Gemini"))
    out = await _handlers()["query_gemini"](
        prompt="Hi", system_prompt="be brief", temperature=0.4, max_tokens=200
    )
    assert "Hello from Gemini" in out
    assert "model=gemini-2.5-flash" in out
    assert captured["path"] == "/models/gemini-2.5-flash:generateContent"
    body = captured["json"]
    assert body["contents"][0]["parts"][0]["text"] == "Hi"
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["generationConfig"]["temperature"] == 0.4
    assert body["generationConfig"]["maxOutputTokens"] == 200


@pytest.mark.asyncio
async def test_query_normalizes_models_prefix(monkeypatch):
    captured = _patch(monkeypatch, data=_gen("ok"))
    await _handlers()["query_gemini"](prompt="Hi", model="models/gemini-x")
    assert captured["path"] == "/models/gemini-x:generateContent"


@pytest.mark.asyncio
async def test_query_sanitizes_crafted_model_into_single_path_segment(monkeypatch):
    # The model id is interpolated into the request path, so anything past the
    # leading [A-Za-z0-9._-] run (here ':' and '/') must be stripped to keep the
    # call pinned to the intended :generateContent endpoint.
    captured = _patch(monkeypatch, data=_gen("ok"))
    await _handlers()["query_gemini"](
        prompt="Hi", model="gemini-2.5-flash:batchGenerateContent/../evil"
    )
    assert captured["path"] == "/models/gemini-2.5-flash:generateContent"


@pytest.mark.asyncio
async def test_query_all_dot_model_falls_back_to_default(monkeypatch):
    captured = _patch(monkeypatch, data=_gen("ok"))
    await _handlers()["query_gemini"](prompt="Hi", model="../")
    assert captured["path"] == "/models/gemini-2.5-flash:generateContent"


@pytest.mark.asyncio
async def test_query_requires_prompt(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely (the tool-call-missing-field case): the handler defaults
    # 'prompt' so validation runs instead of raising a TypeError.
    assert "required" in (await _handlers()["query_gemini"]()).lower()
    # A blank string is rejected the same way.
    assert "required" in (await _handlers()["query_gemini"](prompt="   ")).lower()


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits():
    out = await _handlers(token="")["query_gemini"](prompt="hi")
    assert "api key is missing" in out.lower()


@pytest.mark.asyncio
async def test_list_models_filters_by_generate_support(monkeypatch):
    _patch(
        monkeypatch,
        data={
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        },
    )
    out = await _handlers()["list_gemini_models"]()
    assert "gemini-2.5-flash" in out
    assert "text-embedding-004" not in out


@pytest.mark.asyncio
async def test_evaluate_uses_judge_system_and_zero_temperature(monkeypatch):
    captured = _patch(monkeypatch, data=_gen("SCORE: 9"))
    out = await _handlers()["evaluate_with_gemini"](
        content="answer", criteria="accuracy", scale="1-5", reference="truth"
    )
    assert "SCORE: 9" in out
    body = captured["json"]
    assert body["generationConfig"]["temperature"] == 0.0
    assert "1-5" in body["systemInstruction"]["parts"][0]["text"]
    user = body["contents"][0]["parts"][0]["text"]
    assert "truth" in user
    assert "answer" in user


@pytest.mark.asyncio
async def test_evaluate_requires_inputs(monkeypatch):
    _no_call(monkeypatch)
    # Omitted entirely: defaults let the validation return the friendly error
    # rather than raising a TypeError on the missing positional arguments.
    assert "required" in (await _handlers()["evaluate_with_gemini"]()).lower()
    # A blank field is rejected the same way.
    out = await _handlers()["evaluate_with_gemini"](content="", criteria="x")
    assert "required" in out.lower()


@pytest.mark.asyncio
async def test_compare_models_runs_all(monkeypatch):
    calls: list[str] = []

    async def _fake(api_key, method, path, action, *, json=None, params=None, client=None):
        calls.append(path)
        return _gen(f"answer at {path}"), None

    monkeypatch.setattr(gemini, "_request", _fake)
    out = await _handlers()["compare_gemini_models"](prompt="hi", models=["m1", "m2"])
    assert "m1" in out
    assert "m2" in out
    assert "/models/m1:generateContent" in calls
    assert "/models/m2:generateContent" in calls


@pytest.mark.asyncio
async def test_compare_requires_models(monkeypatch):
    _no_call(monkeypatch)
    # 'models' omitted entirely: defaults to None and coerces to [], so the
    # handler returns the friendly error instead of raising a TypeError.
    assert "at least one model" in (
        await _handlers()["compare_gemini_models"](prompt="hi")
    ).lower()
    # An explicit empty list is rejected the same way.
    out = await _handlers()["compare_gemini_models"](prompt="hi", models=[])
    assert "at least one model" in out.lower()


@pytest.mark.asyncio
async def test_grounded_sets_tool_and_extracts_sources(monkeypatch):
    data = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Acme is big."}]},
                "groundingMetadata": {
                    "webSearchQueries": ["Acme news"],
                    "groundingChunks": [
                        {"web": {"uri": "https://x.com", "title": "X"}}
                    ],
                },
            }
        ]
    }
    captured = _patch(monkeypatch, data=data)
    out = await _handlers()["query_gemini_grounded"](query="Acme?", max_tokens=300)
    assert "Acme is big." in out
    assert "https://x.com" in out
    assert "Acme news" in out
    body = captured["json"]
    assert body["tools"] == [{"google_search": {}}]
    assert body["generationConfig"]["maxOutputTokens"] == 300


@pytest.mark.asyncio
async def test_max_tokens_without_text_returns_actionable_message(monkeypatch):
    # Gemini 2.5 thinking models can spend the whole maxOutputTokens budget on
    # internal reasoning and return an empty candidate with MAX_TOKENS. The tool
    # must surface a self-correcting hint (raise max_tokens) rather than a silent
    # "(no text content returned)".
    _patch(
        monkeypatch,
        data={
            "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0},
        },
    )
    out = await _handlers()["query_gemini"](prompt="Hi")
    lowered = out.lower()
    assert "max_tokens" in lowered
    assert "thinking" in lowered
    assert "truncated" in lowered  # footer note still present
