"""Regression tests for two bugs reported on 2026-05-19:

1. OpenAI ``reasoning_effort='max'`` remapping — gpt-5.2 (and other gpt-5+/
   o-series models) reject ``reasoning_effort='max'`` with HTTP 400 because
   OpenAI's supported values are ``none``, ``low``, ``medium``, ``high``,
   and ``xhigh``.  ``max`` is Anthropic-specific.  The fix adds
   ``normalize_openai_reasoning_effort()`` that silently remaps
   ``max`` → ``xhigh`` before calling the API, and now sends each model the
   nearest level it declares (none at all on a model that takes no effort).

2. Gemini fallback ``NoneType`` crash — when the primary provider fails and
   the Gemini fallback is invoked, ``chat_stream_with_tools`` / ``chat_stream``
   iterate over ``chunk.candidates[0].content.parts`` without a ``None``
   guard.  Certain terminal chunks (e.g. finish-reason-only) have ``parts``
   set to ``None``, causing ``'NoneType' object is not iterable``.  The fix
   adds ``or []`` to every such iteration.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import ChatMessage
from app.llm.gemini_provider import GeminiProvider
from app.llm.openai_compat import (
    _log_effort_replaced_by_none,
    consume_openai_stream_with_tools,
    normalize_openai_reasoning_effort,
)
from app.llm.openai_provider import OpenAIProvider
from app.tools.base import ToolDefinition

# ---------------------------------------------------------------------------
# Part 1 — normalize_openai_reasoning_effort unit tests
# ---------------------------------------------------------------------------


def test_normalize_openai_reasoning_effort_remaps_max_to_xhigh():
    assert normalize_openai_reasoning_effort("max") == "xhigh"


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_normalize_openai_reasoning_effort_preserves_valid_values(effort):
    assert normalize_openai_reasoning_effort(effort) == effort


def test_normalize_openai_reasoning_effort_preserves_unknown_value():
    """Unknown values are forwarded as-is so the API can return a clear error."""
    assert normalize_openai_reasoning_effort("ultra") == "ultra"


def test_normalize_openai_reasoning_effort_max_native_on_gpt6():
    """GPT-6 (Astra) supports ``max`` natively — no remap to ``xhigh``."""
    assert normalize_openai_reasoning_effort("max", "gpt-6-astra") == "max"
    assert normalize_openai_reasoning_effort("max", "gpt-6-astra-2026-09-01") == "max"


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-6-astra",  # OpenRouter vendor prefix
        "openrouter/openai/gpt-6-astra",  # gateway + vendor prefix
        "gpt-6-astra:free",  # variant suffix
    ],
)
def test_normalize_openai_reasoning_effort_max_native_on_prefixed_gpt6(model):
    """Gateway/vendor-prefixed GPT-6 ids keep native ``max`` too.

    The shared ``OpenAICompatibleProvider`` passes the exact advertised id
    (``openai/gpt-6-astra`` via OpenRouter, aliases via LiteLLM) into the
    remap — the family check must normalize the id first or a configured
    ``max`` is silently downgraded to ``xhigh`` on those routes."""
    assert normalize_openai_reasoning_effort("max", model) == "max"


@pytest.mark.parametrize("model", ["gpt-5.2", "gpt-5.5", "openai/gpt-5.4", "grok-4.6", None])
def test_normalize_openai_reasoning_effort_max_still_remapped_elsewhere(model):
    """A model whose top level is ``xhigh`` (and the model-less call) keeps the remap."""
    assert normalize_openai_reasoning_effort("max", model) == "xhigh"


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "openai/gpt-5.6-terra"])
def test_normalize_openai_reasoning_effort_max_native_on_gpt_5_6(model):
    assert normalize_openai_reasoning_effort("max", model) == "max"


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("o3", "max", "high"),
        ("o3", "xhigh", "high"),
        ("gpt-5", "max", "high"),
        ("gpt-5.1", "xhigh", "high"),
        ("gpt-5-mini", "medium", "medium"),
        ("grok-4.5", "xhigh", "high"),
        ("grok-3-mini", "medium", "low"),
        ("mistral-small-latest", "max", "high"),
        ("mistral-medium-2604", "high", "high"),
    ],
)
def test_normalize_openai_reasoning_effort_steps_down_to_a_declared_level(model, effort, expected):
    """A level the model refuses is an HTTP 400: the nearest declared one below is sent."""
    assert normalize_openai_reasoning_effort(effort, model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4.1-mini",
        "chatgpt-4o-latest",
        "gpt-5-chat-latest",
        "gpt-5.3-chat-latest",
        "openai/gpt-4o",
        "grok-4.3",
        "grok-4.20",
        "grok-build-0.1",
        "mistral-large-latest",
        "codestral-latest",
        "magistral-medium-latest",
    ],
)
def test_normalize_openai_reasoning_effort_omitted_on_models_that_take_none(model):
    """These models refuse ``reasoning_effort`` outright, so no level is sent at all."""
    assert normalize_openai_reasoning_effort("high", model) is None
    assert normalize_openai_reasoning_effort("max", model) is None


def test_normalize_openai_reasoning_effort_mistral_below_high_means_no_reasoning():
    """Mistral reasoning is ``high`` or off: a lower level sends nothing."""
    assert normalize_openai_reasoning_effort("low", "mistral-small-latest") is None
    assert normalize_openai_reasoning_effort("medium", "mistral-medium-latest") is None


def test_normalize_openai_reasoning_effort_passes_client_values_outside_the_platform_levels():
    """A proxy client's own ``minimal`` / ``none`` is the model's to judge."""
    assert normalize_openai_reasoning_effort("minimal", "gpt-5") == "minimal"
    assert normalize_openai_reasoning_effort("none", "gpt-5.2") == "none"


@pytest.mark.parametrize("effort", [None, ""])
def test_normalize_openai_reasoning_effort_without_effort_sends_none(effort):
    assert normalize_openai_reasoning_effort(effort, "gpt-5.2") is None


# ---------------------------------------------------------------------------
# Part 1b — Gemini thinking dialect (budget for 2.5, level for 3.x)
# ---------------------------------------------------------------------------


def test_gemini_thinking_config_uses_budget_for_2_5():
    provider = GeminiProvider.__new__(GeminiProvider)
    cfg = provider._build_thinking_config("gemini-2.5-flash", "high")
    assert cfg.thinking_budget == 8192
    assert cfg.thinking_level is None


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("minimal", "LOW"),  # Google's own 3.7 -> 3.8 migration mapping
        ("low", "LOW"),
        ("medium", "MEDIUM"),
        ("high", "HIGH"),
        ("xhigh", "HIGH"),
        ("max", "HIGH"),
        ("unknown-tier", "HIGH"),
    ],
)
def test_gemini_thinking_config_uses_levels_for_3_x(effort, expected):
    """Gemini 3.x rejects ``thinking_budget`` (unsupported) and ``minimal``
    (HTTP 400 since 3.8) — the provider must emit ``thinking_level`` instead."""
    from google.genai import types

    provider = GeminiProvider.__new__(GeminiProvider)
    cfg = provider._build_thinking_config("gemini-3.8-flash", effort)
    assert cfg.thinking_budget is None
    assert cfg.thinking_level == getattr(types.ThinkingLevel, expected)


# ---------------------------------------------------------------------------
# Part 2 — OpenAIProvider sends remapped reasoning_effort
# ---------------------------------------------------------------------------


def _make_openai_chat_response(model: str = "gpt-5.2") -> Any:
    """Minimal fake OpenAI chat completion response."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="ok",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )
        ],
        model=model,
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
    )


def test_openai_provider_chat_remaps_max_to_xhigh():
    """``OpenAIProvider.chat`` must remap ``reasoning_effort='max'`` to ``'xhigh'``."""
    provider = OpenAIProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _make_openai_chat_response()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())
    )

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hi")],
            model="gpt-5.2",
            reasoning_effort="max",
        )
    )

    assert captured.get("reasoning_effort") == "xhigh", (
        "reasoning_effort='max' must be remapped to 'xhigh' for OpenAI models"
    )


def test_openai_provider_chat_stream_remaps_max_to_xhigh():
    """``OpenAIProvider.chat_stream`` must remap ``reasoning_effort='max'``."""
    provider = OpenAIProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeStream:
        def __aiter__(self):
            self._done = False
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok", reasoning_content=None),
                        finish_reason="stop",
                    )
                ]
            )

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())
    )

    async def _collect():
        chunks = []
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")],
            model="gpt-5.2",
            reasoning_effort="max",
        ):
            chunks.append(chunk)
        return chunks

    asyncio.run(_collect())

    assert captured.get("reasoning_effort") == "xhigh"


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-5-chat-latest"])
def test_openai_provider_chat_sends_no_reasoning_effort_to_a_model_that_refuses_it(model):
    """An agent's configured effort must not turn into an HTTP 400 on these models."""
    provider = OpenAIProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _make_openai_chat_response(model)

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    asyncio.run(provider.chat([ChatMessage(role="user", content="hi")], model=model, reasoning_effort="high"))

    assert "reasoning_effort" not in captured


@pytest.mark.parametrize("model", ["gpt-6", "openai/gpt-6-luna"])
def test_normalize_openai_reasoning_effort_is_none_on_gpt6_with_tools(model):
    """GPT-6 answers HTTP 400 to an effort but ``none`` next to tools (#11571)."""
    assert normalize_openai_reasoning_effort("high", model, has_tools=True) == "none"
    assert normalize_openai_reasoning_effort("none", model, has_tools=True) == "none"
    assert normalize_openai_reasoning_effort(None, model, has_tools=True) == "none"
    assert normalize_openai_reasoning_effort(None, model) is None
    assert normalize_openai_reasoning_effort("high", model) == "high"
    assert normalize_openai_reasoning_effort("high", "gpt-5.6", has_tools=True) == "high"


def test_replaced_effort_is_logged_once_on_gpt6_with_tools(caplog):
    """A platform level dropped to ``none`` is logged once; unset, ``none`` or a client's own value is not."""
    _log_effort_replaced_by_none.cache_clear()
    with caplog.at_level(logging.INFO, logger="app.llm.openai_compat"):
        for effort in (None, "none", "high\nforged line"):
            assert normalize_openai_reasoning_effort(effort, "gpt-6", has_tools=True) == "none"
        assert not caplog.records
        normalize_openai_reasoning_effort("high", "gpt-6", has_tools=True)
        normalize_openai_reasoning_effort("high", "gpt-6", has_tools=True)
    assert [r.getMessage() for r in caplog.records] == [
        "reasoning_effort high replaced by none on 'gpt-6': it refuses an effort with tools"
    ]


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-4o"])
def test_normalize_openai_reasoning_effort_sends_no_default_effort_with_tools(model):
    """Only a model flagged ``effort_refused_with_tools`` gets ``none`` when no effort is set."""
    assert normalize_openai_reasoning_effort(None, model, has_tools=True) is None


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup", description="Look up", input_schema={"type": "object", "properties": {}}
    )


@pytest.mark.parametrize("effort", ["high", None])
def test_openai_provider_chat_sends_effort_none_to_gpt6_with_tools(effort):
    provider = OpenAIProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _make_openai_chat_response("gpt-6")

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hi")],
            model="gpt-6",
            tools=[_tool()],
            reasoning_effort=effort,
        )
    )

    assert captured["tools"]
    assert captured["reasoning_effort"] == "none"


@pytest.mark.parametrize("effort", ["high", None])
def test_streamed_tool_call_sends_effort_none_to_gpt6(effort):
    captured: dict[str, Any] = {}

    class _EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _EmptyStream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    asyncio.run(
        consume_openai_stream_with_tools(
            client,
            model="gpt-6",
            oai_messages=[{"role": "user", "content": "hi"}],
            tools=[_tool()],
            reasoning_effort=effort,
        )
    )

    assert captured["tools"]
    assert captured["reasoning_effort"] == "none"


# ---------------------------------------------------------------------------
# Part 3 — GeminiProvider handles None content.parts without crashing
# ---------------------------------------------------------------------------

def _make_gemini_finish_chunk() -> Any:
    """A Gemini streaming chunk that has a candidate but ``content.parts = None``.

    This represents a terminal chunk (finish-reason-only) where the
    SDK sets ``content`` to a truthy object but leaves ``parts`` unset.
    """
    content = SimpleNamespace(parts=None)
    candidate = SimpleNamespace(
        finish_reason=SimpleNamespace(__str__=lambda self: "STOP"),
        content=content,
    )
    return SimpleNamespace(candidates=[candidate], text=None)


def _make_gemini_text_chunk(text: str) -> Any:
    part = SimpleNamespace(
        text=text,
        thought=False,
        function_call=None,
    )
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(
        finish_reason=None,
        content=content,
    )
    return SimpleNamespace(candidates=[candidate], text=None)


class _FakeGeminiStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk


def test_gemini_chat_stream_handles_none_parts():
    """``chat_stream`` must not crash when a chunk has ``content.parts = None``."""
    provider = GeminiProvider(api_key="dummy")
    chunks = [
        _make_gemini_text_chunk("hello"),
        _make_gemini_finish_chunk(),  # parts=None — triggered the NoneType crash
    ]

    class _FakeModels:
        async def generate_content_stream(self, **kwargs):
            return _FakeGeminiStream(chunks)

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))

    async def _collect():
        result = []
        async for token in provider.chat_stream(
            [ChatMessage(role="user", content="hi")]
        ):
            result.append(token)
        return result

    tokens = asyncio.run(_collect())
    assert tokens == ["hello"]


def test_gemini_chat_stream_with_tools_handles_none_parts():
    """``chat_stream_with_tools`` must not crash when a chunk has ``content.parts = None``."""
    provider = GeminiProvider(api_key="dummy")
    chunks = [
        _make_gemini_text_chunk("world"),
        _make_gemini_finish_chunk(),
    ]

    class _FakeModels:
        async def generate_content_stream(self, **kwargs):
            return _FakeGeminiStream(chunks)

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hi")]
        )
    )

    assert response.content == "world"
    assert response.stop_reason == "end_turn"


def test_gemini_tool_call_ids_are_deterministic_per_response():
    """Pin the id scheme the transcript code must live with: Gemini mints
    ``call_{function_name}_{index}`` per RESPONSE, so two turns whose first
    call is the same function share a ``tool_call_id``. Anything pairing calls
    with results across turns (``agent_loop.collect_task_labels``) therefore
    has to pair in transcript order, never through a global id map."""
    provider = GeminiProvider(api_key="dummy")
    part = SimpleNamespace(
        text=None,
        thought=False,
        function_call=SimpleNamespace(
            name="spawn_background_task", args={"task": "x"}
        ),
    )
    candidate = SimpleNamespace(
        finish_reason=None, content=SimpleNamespace(parts=[part])
    )
    response = SimpleNamespace(candidates=[candidate], usage_metadata=None)

    class _FakeModels:
        async def generate_content(self, **kwargs):
            return response

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))

    async def _two_turns():
        first = await provider.chat([ChatMessage(role="user", content="a")])
        second = await provider.chat([ChatMessage(role="user", content="b")])
        return first, second

    first, second = asyncio.run(_two_turns())
    assert first.tool_calls and second.tool_calls
    assert first.tool_calls[0].id == "call_spawn_background_task_0"
    assert second.tool_calls[0].id == first.tool_calls[0].id
