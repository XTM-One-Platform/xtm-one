"""A model with no valid chat-completions request with tools goes through the Responses API (#12205).

gpt-6-astra refuses an effort next to function tools on ``/v1/chat/completions``
("use /v1/responses or set reasoning_effort to 'none'") and refuses ``none``:
the native OpenAI provider sends its requests with tools to ``/v1/responses``
with its lowest level. Elsewhere the two refusals end the leg on one named
400, and an exhausted chain names every leg, a routed primary included.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, ModelDeclinedError
from app.llm.model_capabilities import uses_responses_api_with_tools
from app.llm.openai_compat import FUNCTION_TOOLS_UNSUPPORTED, create_with_effort_retry
from app.llm.openai_provider import OpenAIProvider
from app.llm.openai_responses import (
    build_responses_params,
    parse_responses_output,
    responses_effort,
    responses_input,
)
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider
from app.tools.base import ToolDefinition

_ASTRA = "gpt-6-astra"
_TOOLS_REFUSAL = (
    "Function tools with reasoning_effort are not supported for this model in /v1/chat/completions. "
    "To use function tools, use /v1/responses or set reasoning_effort to 'none'."
)
_NONE_REFUSAL = (
    "Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
    "Supported values are: 'low', 'medium', 'high', and 'xhigh'."
)


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"message": message, "param": "reasoning_effort", "code": "unsupported_value"}
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=body)


def _tool() -> ToolDefinition:
    return ToolDefinition(name="lookup", description="Look up", input_schema={"type": "object", "properties": {}})


def _responses_answer(output: list[Any], model: str = _ASTRA, incomplete: str | None = None) -> Any:
    return SimpleNamespace(
        output=output,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            input_tokens_details=SimpleNamespace(cached_tokens=3),
            output_tokens_details=SimpleNamespace(reasoning_tokens=2),
        ),
        model=model,
        incomplete_details=SimpleNamespace(reason=incomplete) if incomplete else None,
    )


def _text(text: str) -> Any:
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


class _Endpoints:
    """Records every request on both endpoints; chat completions refuse as gpt-6-astra does."""

    def __init__(self, answer: Any = None, events: list[Any] | None = None) -> None:
        self.chat_calls: list[dict] = []
        self.responses_calls: list[dict] = []
        self.answer = answer if answer is not None else _responses_answer([_text("ok")])
        self.events = events
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.chat_calls.append(kwargs)
                raise _bad_request(_NONE_REFUSAL if kwargs.get("reasoning_effort") == "none" else _TOOLS_REFUSAL)

        class _Responses:
            async def create(self, **kwargs):
                outer.responses_calls.append(kwargs)
                if kwargs.get("stream"):
                    return _aiter(outer.events or [])
                return outer.answer

        self.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()), responses=_Responses())


async def _aiter(items: list[Any]):
    for item in items:
        yield item


def _provider(endpoints: _Endpoints) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="dummy")
    provider.client = endpoints.client
    provider.default_model = _ASTRA
    return provider


@pytest.mark.parametrize(
    "model", [_ASTRA, "gpt-6-astra-2026-09-01", "openai/gpt-6-astra", "gpt-6.2-sol", "gpt-6.2-astra"]
)
def test_gpt6_astra_is_flagged_for_the_responses_api(model):
    assert uses_responses_api_with_tools(model)


@pytest.mark.parametrize("model", ["gpt-6", "gpt-6-luna", "gpt-5.2", "gpt-4o", "claude-opus-5-5", None])
def test_other_models_are_not_flagged(model):
    assert not uses_responses_api_with_tools(model)


@pytest.mark.parametrize("effort", ["none", "minimal", None])
async def test_a_gpt6_astra_leg_with_tools_sends_a_responses_request_with_low(effort):
    endpoints = _Endpoints()
    response = await _provider(endpoints).chat(
        [ChatMessage(role="user", content="hi")], model=_ASTRA, tools=[_tool()], reasoning_effort=effort
    )

    assert response.content == "ok"
    assert endpoints.chat_calls == []
    [params] = endpoints.responses_calls
    assert params["reasoning"] == {"effort": "low", "summary": "auto"}
    assert [tool["name"] for tool in params["tools"]] == ["lookup"]
    assert params["tools"][0]["type"] == "function"
    assert "reasoning_effort" not in params
    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def test_a_level_the_model_takes_is_kept():
    for effort in ("low", "medium", "high", "xhigh"):
        assert responses_effort(effort, _ASTRA) == effort


async def test_a_tool_call_is_read_back_as_on_the_chat_path():
    call = SimpleNamespace(type="function_call", call_id="call_1", id="fc_1", name="lookup", arguments='{"q": "x"}')
    reasoning = SimpleNamespace(type="reasoning", summary=[SimpleNamespace(type="summary_text", text="thinking")])
    endpoints = _Endpoints(answer=_responses_answer([reasoning, call]))

    response = await _provider(endpoints).chat([ChatMessage(role="user", content="hi")], model=_ASTRA, tools=[_tool()])

    assert response.stop_reason == "tool_use"
    assert [(tc.id, tc.name, tc.arguments) for tc in response.tool_calls] == [("call_1", "lookup", {"q": "x"})]
    assert response.thinking == "thinking"


def test_a_tool_round_is_sent_as_function_call_items():
    items = responses_input(
        [
            {"role": "system", "content": "be brief"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:x"}}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "found"},
        ]
    )
    assert items == [
        {"role": "system", "content": "be brief"},
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "data:x"}],
        },
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "found"},
    ]


def test_structured_output_and_max_tokens_are_mapped():
    schema = {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}, "strict": True}}
    params = build_responses_params(
        _ASTRA, [], [_tool()], temperature=0.2, max_tokens=500, reasoning_effort=None, structured_output=schema
    )
    assert params["text"] == {
        "format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}, "strict": True}
    }
    assert params["max_output_tokens"] == 500
    assert params["store"] is False


def test_a_cut_answer_reads_max_tokens_and_a_filtered_one_a_decline():
    assert (
        parse_responses_output(_responses_answer([_text("par")], incomplete="max_output_tokens"), _ASTRA).stop_reason
        == "max_tokens"
    )
    with pytest.raises(ModelDeclinedError):
        parse_responses_output(_responses_answer([], incomplete="content_filter"), _ASTRA)


async def test_the_stream_reaches_on_token_and_ends_on_the_completed_response():
    final = _responses_answer([_text("hello")])
    events = [
        SimpleNamespace(type="response.reasoning_summary_text.delta", delta="hm"),
        SimpleNamespace(type="response.output_text.delta", delta="hel"),
        SimpleNamespace(type="response.output_text.delta", delta="lo"),
        SimpleNamespace(type="response.completed", response=final),
    ]
    endpoints = _Endpoints(events=events)
    tokens: list[str] = []
    thoughts: list[str] = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    async def on_thinking(text: str) -> None:
        thoughts.append(text)

    response = await _provider(endpoints).chat_stream_with_tools(
        [ChatMessage(role="user", content="hi")],
        tools=[_tool()],
        on_token=on_token,
        model=_ASTRA,
        on_thinking=on_thinking,
    )

    assert response.content == "hello"
    assert tokens == ["hel", "lo"]
    assert thoughts == ["hm"]
    assert endpoints.chat_calls == []
    assert endpoints.responses_calls[0]["stream"] is True


async def test_a_request_without_tools_stays_on_chat_completions():
    endpoints = _Endpoints()
    with pytest.raises(openai.BadRequestError):
        await _provider(endpoints).chat([ChatMessage(role="user", content="hi")], model=_ASTRA)
    assert endpoints.responses_calls == []
    assert endpoints.chat_calls


async def test_the_double_refusal_ends_in_one_named_cause_in_two_calls():
    calls: list[dict] = []

    async def create(params):
        calls.append(params)
        raise _bad_request(_NONE_REFUSAL if params.get("reasoning_effort") == "none" else _TOOLS_REFUSAL)

    with pytest.raises(openai.BadRequestError) as caught:
        await create_with_effort_retry(create, {"model": "gpt-7-nova", "reasoning_effort": "low"}, "gpt-7-nova")

    assert len(calls) == 2
    assert caught.value.code == FUNCTION_TOOLS_UNSUPPORTED
    assert caught.value.status_code == 400
    assert "function tools unsupported on chat completions for gpt-7-nova" in str(caught.value)
    assert FailoverLLMProvider._is_non_failoverable(caught.value) is False


class _Anthropic(BaseLLMProvider):
    """A primary whose models both decline (cyber), as the chain of the incident."""

    provider_name = "anthropic"
    context_window = 100_000
    default_model = "claude-opus-5-5"

    async def chat(self, messages, model=None, **kwargs):
        requested = model or self.default_model
        raise ModelDeclinedError(model=requested, category="cyber")

    async def chat_stream(self, messages, model=None, **kwargs):
        raise ModelDeclinedError(model=model or self.default_model, category="cyber")
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _no_backoff(monkeypatch):
    async def _instant() -> None:
        return None

    monkeypatch.setattr("app.llm.router._same_provider_backoff", _instant)


@pytest.mark.usefixtures("_no_backoff")
async def test_failover_from_two_declined_anthropic_legs_to_gpt6_astra_with_tools_answers():
    endpoints = _Endpoints()
    wrapper = FailoverLLMProvider(primary=_Anthropic(), fallbacks=[_provider(endpoints)])

    response = await wrapper.chat(
        [ChatMessage(role="user", content="next step")],
        model="claude-opus-5-5",
        tools=[_tool()],
        reasoning_effort="none",
        disable_thinking=True,
    )

    assert isinstance(response, ChatResponse) and response.content == "ok"
    assert endpoints.chat_calls == []
    assert endpoints.responses_calls[0]["reasoning"]["effort"] == "low"


@pytest.mark.usefixtures("_no_backoff")
async def test_an_exhausted_chain_names_three_legs():
    endpoints = _Endpoints()

    async def _rate_limited(**kwargs):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        raise openai.RateLimitError("Rate limit reached", response=response, body={"code": "rate_limit_exceeded"})

    endpoints.client.responses.create = _rate_limited
    fallback = _provider(endpoints)
    wrapper = FailoverLLMProvider(primary=_Anthropic(), fallbacks=[fallback])

    with pytest.raises(AllProvidersFailedError) as caught:
        await wrapper.chat([ChatMessage(role="user", content="next step")], model="claude-opus-5-5", tools=[_tool()])

    assert [leg.model for leg in caught.value.legs] == ["claude-opus-5-5", "claude-opus-4-8", _ASTRA]


@pytest.mark.usefixtures("_no_backoff")
async def test_a_primary_routed_to_its_decline_fallback_names_the_model_asked():
    endpoints = _Endpoints()
    wrapper = FailoverLLMProvider(primary=_Anthropic(), fallbacks=[_provider(endpoints)])
    wrapper._decline_routes["claude-opus-5-5"] = "claude-opus-4-8"

    async def _rate_limited(**kwargs):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        raise openai.RateLimitError("Rate limit reached", response=response, body={"code": "rate_limit_exceeded"})

    endpoints.client.responses.create = _rate_limited

    with pytest.raises(AllProvidersFailedError) as caught:
        await wrapper.chat([ChatMessage(role="user", content="next step")], model="claude-opus-5-5", tools=[_tool()])

    first = caught.value.legs[0]
    assert (first.model, first.requested) == ("claude-opus-4-8", "claude-opus-5-5")
    assert "claude-opus-4-8 (for claude-opus-5-5): declined (cyber)" in str(caught.value)
    assert first.as_dict()["requested"] == "claude-opus-5-5"
