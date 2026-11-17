"""A ``temperature`` the target model refuses is never what an OpenAI leg ends on (#12216).

``gpt-6-astra`` answers ``temperature`` with HTTP 400 "Unsupported parameter",
on chat completions and the Responses API: the capability table omits it, and
a 400 that still names it is retried once on the same leg without it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.llm.base import ChatMessage
from app.llm.model_capabilities import build_openai_generation_params, omit_temperature
from app.llm.openai_compat import create_with_effort_retry, create_without_refused_temperature
from app.llm.openai_provider import OpenAIProvider
from app.llm.openai_responses import build_responses_params, responses_chat
from app.llm.router import FailoverLeg
from app.tools.base import ToolDefinition

_REFUSAL = "Unsupported parameter: 'temperature' is not supported with this model."


def _temperature_refusal(url: str = "https://api.openai.com/v1/responses") -> openai.BadRequestError:
    request = httpx.Request("POST", url)
    body = {"message": _REFUSAL, "type": "invalid_request_error", "param": "temperature", "code": None}
    return openai.BadRequestError(_REFUSAL, response=httpx.Response(400, request=request), body=body)


def _tool() -> ToolDefinition:
    return ToolDefinition(name="lookup", description="Look up", input_schema={"type": "object", "properties": {}})


def _answer(model: str) -> Any:
    return SimpleNamespace(
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="ok")])],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        model=model,
        incomplete_details=None,
    )


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-astra-2026-09-01", "openai/gpt-6-astra"])
def test_gpt6_astra_is_never_sent_a_temperature(model: str) -> None:
    assert omit_temperature(model) is True
    assert "temperature" not in build_openai_generation_params(model, temperature=0.0, max_tokens=64)
    params = build_responses_params(model, [], [_tool()], temperature=0.0, max_tokens=64, reasoning_effort=None)
    assert "temperature" not in params


async def test_a_gpt6_astra_chat_completions_request_carries_no_temperature() -> None:
    calls: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                model="gpt-6-astra",
            )

    provider = OpenAIProvider(api_key="dummy")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    response = await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-6-astra", temperature=0.0)

    assert response.content == "ok"
    assert "temperature" not in calls[0]


@pytest.mark.parametrize("stream", [False, True])
async def test_a_responses_refusal_is_retried_once_without_temperature(stream: bool) -> None:
    """A model the table does not flag keeps its leg: one retry, no temperature."""
    calls: list[dict[str, Any]] = []

    async def _events() -> AsyncIterator[Any]:
        yield SimpleNamespace(type="response.completed", response=_answer("gpt-7-nova"))

    class _Responses:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if "temperature" in kwargs:
                raise _temperature_refusal()
            return _events() if kwargs.get("stream") else _answer("gpt-7-nova")

    response = await responses_chat(
        SimpleNamespace(responses=_Responses()),
        model="gpt-7-nova",
        oai_messages=[{"role": "user", "content": "hi"}],
        tools=[_tool()],
        temperature=0.3,
        is_retryable=lambda exc: False,
        stream=stream,
    )

    assert response.content == "ok"
    assert [("temperature" in call) for call in calls] == [True, False]


async def test_a_chat_completions_refusal_is_retried_once_without_temperature() -> None:
    calls: list[dict[str, Any]] = []

    async def create(params: dict[str, Any]) -> str:
        calls.append(params)
        if "temperature" in params:
            raise _temperature_refusal("https://api.openai.com/v1/chat/completions")
        return "ok"

    assert (
        await create_with_effort_retry(create, {"model": "m", "temperature": 0.2, "reasoning_effort": "low"}, "m")
        == "ok"
    )
    assert calls == [
        {"model": "m", "temperature": 0.2, "reasoning_effort": "low"},
        {"model": "m", "reasoning_effort": "low"},
    ]


async def test_another_400_or_a_request_without_temperature_is_not_retried() -> None:
    calls: list[dict[str, Any]] = []

    async def create(params: dict[str, Any]) -> str:
        calls.append(params)
        raise _temperature_refusal()

    with pytest.raises(openai.BadRequestError):
        await create_without_refused_temperature(create, {"model": "m"}, "m")
    assert len(calls) == 1

    async def other(params: dict[str, Any]) -> str:
        calls.append(params)
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        raise openai.BadRequestError("bad", response=httpx.Response(400, request=request), body={"param": "tools"})

    calls.clear()
    with pytest.raises(openai.BadRequestError):
        await create_without_refused_temperature(other, {"model": "m", "temperature": 0.2}, "m")
    assert len(calls) == 1


def test_the_chain_error_names_the_refused_parameter() -> None:
    leg = FailoverLeg.of("openai", _temperature_refusal(), model="gpt-6-astra")
    assert leg.status == 400
    assert leg.code == "temperature not supported"
    assert leg.outcome() == "HTTP 400 temperature not supported"
