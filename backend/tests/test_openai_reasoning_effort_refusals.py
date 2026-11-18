"""A ``reasoning_effort`` the target model refuses is never what an OpenAI leg sends (#12162).

``gpt-6-astra`` accepts ``low`` to ``xhigh`` and answers ``none`` with HTTP 400
``unsupported_value``; the capability table maps a caller's ``none`` to the
model's lowest level, and a 400 that still names ``reasoning_effort`` is
retried once on the same leg with a value the error lists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.llm.base import BaseLLMProvider, ChatMessage
from app.llm.model_capabilities import lowest_effort, refuses_effort_none
from app.llm.openai_compat import (
    _log_effort_refused,
    create_with_effort_retry,
    normalize_openai_reasoning_effort,
)
from app.llm.openai_provider import OpenAIProvider
from app.llm.router import FailoverLLMProvider
from app.tools.base import ToolDefinition

_REFUSED_NONE = (
    "Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
    "Supported values are: 'low', 'medium', 'high', and 'xhigh'."
)


def _bad_request(message: str, param: str | None = "reasoning_effort") -> openai.BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"message": message, "param": param, "code": "unsupported_value"}
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=body)


def _response(model: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=model,
    )


def _tool() -> ToolDefinition:
    return ToolDefinition(name="lookup", description="Look up", input_schema={"type": "object", "properties": {}})


def _provider_capturing(model: str, refuse: set[str | None] | None = None) -> tuple[OpenAIProvider, list[dict]]:
    """An OpenAI provider whose API records every request and refuses the efforts in *refuse*."""
    provider = OpenAIProvider(api_key="dummy")
    calls: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if refuse is not None and kwargs.get("reasoning_effort", "<unset>") in refuse:
                raise _bad_request(_REFUSED_NONE)
            return _response(model)

    class _Responses:
        async def create(self, **kwargs):
            calls.append({"endpoint": "responses", **kwargs})
            return SimpleNamespace(
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="ok")])],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model=model,
                incomplete_details=None,
            )

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()), responses=_Responses())
    return provider, calls


@pytest.mark.parametrize(
    "model", ["gpt-6-astra", "gpt-6-astra-2026-09-01", "openai/gpt-6-astra", "gpt-6.2-sol", "gpt-6.2-astra"]
)
def test_gpt6_astra_is_declared_without_none(model):
    assert refuses_effort_none(model)
    assert lowest_effort(model) == "low"


@pytest.mark.parametrize("effort", ["none", "minimal"])
@pytest.mark.parametrize("has_tools", [False, True])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.2-sol", "gpt-6.2-astra"])
def test_a_model_that_refuses_none_gets_its_lowest_level(effort, has_tools, model):
    assert normalize_openai_reasoning_effort(effort, model, has_tools=has_tools) == "low"


@pytest.mark.parametrize("has_tools", [False, True])
def test_gpt6_astra_with_no_effort_is_sent_none_of_it(has_tools):
    """No effort set: the parameter is left out, never ``none``."""
    assert normalize_openai_reasoning_effort(None, "gpt-6-astra", has_tools=has_tools) is None


def test_gpt6_astra_keeps_the_levels_it_accepts():
    for effort in ("low", "medium", "high", "xhigh"):
        assert normalize_openai_reasoning_effort(effort, "gpt-6-astra", has_tools=True) == effort


def test_a_tools_refusal_asking_for_none_is_retried_with_none():
    """The other 400 (#11571): an effort next to tools, answered 'set reasoning_effort to none'."""
    calls: list[dict] = []

    async def create(params):
        calls.append(params)
        if params.get("reasoning_effort") != "none":
            raise _bad_request(
                "Function tools with reasoning_effort are not supported for this model in "
                "/v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'."
            )
        return "ok"

    assert asyncio.run(create_with_effort_retry(create, {"reasoning_effort": "low"}, "m")) == "ok"
    assert asyncio.run(create_with_effort_retry(create, {}, "m")) == "ok"
    assert [c.get("reasoning_effort") for c in calls] == ["low", "none", None, "none"]


@pytest.mark.parametrize("model", ["gpt-5.2", "gpt-6", "gpt-6-luna", None])
def test_no_change_for_models_that_accept_none(model):
    assert normalize_openai_reasoning_effort("none", model) == "none"


def test_an_openai_leg_never_sends_none_to_gpt6_astra():
    provider, calls = _provider_capturing("gpt-6-astra")
    asyncio.run(provider.chat([ChatMessage(role="user", content="hi")], model="gpt-6-astra", reasoning_effort="none"))
    assert [c.get("reasoning_effort") for c in calls] == ["low"]


def test_a_refused_effort_is_retried_once_with_a_listed_value(caplog):
    """A model the table does not know yet refuses ``none``: the same leg answers on the retry."""
    _log_effort_refused.cache_clear()
    provider, calls = _provider_capturing("gpt-7-nova", refuse={"none"})
    with caplog.at_level(logging.WARNING, logger="app.llm.openai_compat"):
        response = asyncio.run(
            provider.chat([ChatMessage(role="user", content="hi")], model="gpt-7-nova", reasoning_effort="none")
        )
    assert response.content == "ok"
    assert [c.get("reasoning_effort") for c in calls] == ["none", "low"]
    assert [r.getMessage() for r in caplog.records] == [
        "reasoning_effort none refused by 'gpt-7-nova': retried with low"
    ]


def test_the_retry_is_made_once_then_the_error_propagates():
    provider, calls = _provider_capturing("gpt-7-nova", refuse={"none", "low"})
    with pytest.raises(openai.BadRequestError):
        asyncio.run(
            provider.chat([ChatMessage(role="user", content="hi")], model="gpt-7-nova", reasoning_effort="none")
        )
    assert len(calls) == 2


def test_a_400_about_another_parameter_is_not_retried():
    calls: list[dict] = []

    async def create(params):
        calls.append(params)
        raise _bad_request("Unsupported value: 'temperature'.", param="temperature")

    with pytest.raises(openai.BadRequestError):
        asyncio.run(create_with_effort_retry(create, {"reasoning_effort": "none"}, "gpt-7-nova"))
    assert len(calls) == 1


def test_a_refusal_without_listed_values_retries_without_the_parameter():
    calls: list[dict] = []

    async def create(params):
        calls.append(params)
        if "reasoning_effort" in params:
            raise _bad_request("Unsupported parameter: 'reasoning_effort' is not supported with this model.")
        return "ok"

    assert asyncio.run(create_with_effort_retry(create, {"model": "m", "reasoning_effort": "high"}, "m")) == "ok"
    assert calls == [{"model": "m", "reasoning_effort": "high"}, {"model": "m"}]


class _DecliningPrimary(BaseLLMProvider):
    """An Anthropic primary with thinking off that fails, as a declined leg does."""

    provider_name = "anthropic"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        raise RuntimeError("primary failure")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        if False:
            yield ""
        raise RuntimeError("primary failure")

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


def test_a_failover_to_an_openai_reasoning_model_sends_a_valid_effort():
    """Anthropic (thinking off) -> OpenAI gpt-6-astra: the leg maps its own effort, never ``none``."""
    fallback, calls = _provider_capturing("gpt-6-astra", refuse={"none"})
    fallback.default_model = "gpt-6-astra"
    provider = FailoverLLMProvider(primary=_DecliningPrimary(), fallbacks=[fallback])

    response = asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hi")],
            tools=[_tool()],
            reasoning_effort="none",
            disable_thinking=True,
        )
    )

    assert response.content == "ok"
    # With tools, gpt-6-astra goes through the Responses API with a level it takes (#12205).
    assert [(c.get("endpoint"), c.get("reasoning")) for c in calls] == [
        ("responses", {"effort": "low", "summary": "auto"})
    ]
