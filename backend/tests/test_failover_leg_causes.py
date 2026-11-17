"""An exhausted failover chain says, per leg, the model and why it failed.

The error of a chain used to name provider types only ("All configured AI
providers failed (anthropic, anthropic, openai). Please try again"): a decline
read as an outage, the user was told to retry content every model declines,
and an admin could not tell a credential error on a fallback from a quota one
without the backend logs. Each leg now carries its model, its config, its kind
(declined, http_error, timeout, connection, other), the decline category or
the HTTP status and a short, redacted error; the message, the failure
classification, the trace metadata and the platform chat API body show them.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from anthropic._exceptions import OverloadedError

from app.api.v1 import platform as platform_api
from app.api.v1.chat import _friendly_error_message
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, ModelDeclinedError
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider
from app.services.failure_causes import FailureCause, classify_run_failure

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_PRIMARY = "claude-opus-5-5"
_DECLINE_FALLBACK = "claude-opus-4-8"
_PLATFORM_FALLBACK = "gpt-6-astra"


def _rate_limited() -> openai.RateLimitError:
    body = {"error": {"message": "Rate limit reached", "type": "tokens", "param": None, "code": "rate_limit_exceeded"}}
    response = httpx.Response(429, json=body, request=httpx.Request("POST", _OPENAI_URL))
    return openai.RateLimitError(f"Error code: 429 - {body}", response=response, body=body["error"])


def _overloaded() -> OverloadedError:
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = httpx.Response(529, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return OverloadedError(f"Error code: 529 - {body}", response=response, body=body)


class _Provider(BaseLLMProvider):
    context_window = 100_000

    def __init__(self, name: str, default_model: str, errors: dict[str, BaseException], label: str) -> None:
        self.provider_name = name
        self.default_model = default_model
        self.errors = errors
        self.provider_config_id = f"id-{label}"
        self.provider_config_label = label

    def _answer(self, model: str | None) -> str:
        requested = model or self.default_model
        if requested in self.errors:
            raise self.errors[requested]
        return f"answered by {requested}"

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content=self._answer(model), model=model)

    async def chat_stream(self, messages, model=None, **kwargs):
        yield self._answer(model)

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        return ChatResponse(content=self._answer(model), model=model)

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _instant() -> None:
        return None

    monkeypatch.setattr("app.llm.router._same_provider_backoff", _instant)


def _declined_then_throttled() -> FailoverLLMProvider:
    primary = _Provider(
        "anthropic",
        _PRIMARY,
        {
            _PRIMARY: ModelDeclinedError(model=_PRIMARY, category="cyber"),
            _DECLINE_FALLBACK: ModelDeclinedError(model=_DECLINE_FALLBACK, category="cyber"),
        },
        "Anthropic main",
    )
    fallback = _Provider("openai", _PLATFORM_FALLBACK, {_PLATFORM_FALLBACK: _rate_limited()}, "OpenAI fallback")
    return FailoverLLMProvider(primary=primary, fallbacks=[fallback])


async def _chat(wrapper: FailoverLLMProvider) -> str:
    return (await wrapper.chat([ChatMessage(role="user", content="next step")], model=_PRIMARY)).content


async def _stream(wrapper: FailoverLLMProvider) -> str:
    messages = [ChatMessage(role="user", content="next step")]
    return "".join([chunk async for chunk in wrapper.chat_stream(messages, model=_PRIMARY)])


@pytest.mark.parametrize("call", [_chat, _stream], ids=["chat", "chat_stream"])
async def test_a_declined_chain_names_each_model_and_its_cause(call):
    with pytest.raises(AllProvidersFailedError) as caught:
        await call(_declined_then_throttled())

    error = caught.value
    message = str(error)
    assert f"{_PRIMARY}: declined (cyber)" in message
    assert f"{_DECLINE_FALLBACK}: declined (cyber)" in message
    assert f"{_PLATFORM_FALLBACK}: HTTP 429 rate_limit_exceeded" in message
    assert "Please try again" not in message
    assert "will not help" in message
    assert [(leg.model, leg.kind, leg.config) for leg in error.legs] == [
        (_PRIMARY, "declined", "Anthropic main"),
        (_DECLINE_FALLBACK, "declined", "Anthropic main"),
        (_PLATFORM_FALLBACK, "http_error", "OpenAI fallback"),
    ]

    classification = classify_run_failure(error)
    assert classification.cause is FailureCause.ALL_PROVIDERS_FAILED
    for model in (_PRIMARY, _DECLINE_FALLBACK, _PLATFORM_FALLBACK):
        assert model in (classification.detail or "")
    assert [leg["model"] for leg in classification.as_trace_metadata()["failure_legs"]] == [
        _PRIMARY,
        _DECLINE_FALLBACK,
        _PLATFORM_FALLBACK,
    ]
    # The chat says the content was declined, not "try again".
    assert "declined" in _friendly_error_message(error)


async def test_a_chain_of_declines_is_classified_as_a_decline_with_its_categories():
    error = AllProvidersFailedError(
        [
            ("anthropic", ModelDeclinedError(model=_PRIMARY, category="cyber")),
            ("anthropic", ModelDeclinedError(model=_DECLINE_FALLBACK, category="frontier_llm")),
        ]
    )

    classification = classify_run_failure(error)

    assert classification.cause is FailureCause.MODEL_DECLINED
    assert classification.detail == "cyber,frontier_llm"
    assert len(classification.legs) == 2
    assert "Please try again" not in str(error)


async def test_two_overloaded_legs_name_both_models_and_the_status():
    primary = _Provider("anthropic", "claude-sonnet-5", {"claude-sonnet-5": _overloaded()}, "Anthropic main")
    second = _Provider("anthropic", "claude-haiku-5", {"claude-haiku-5": _overloaded()}, "Anthropic backup")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[second])

    with pytest.raises(AllProvidersFailedError) as caught:
        await wrapper.chat([ChatMessage(role="user", content="hi")])

    message = str(caught.value)
    assert "claude-sonnet-5: HTTP 529" in message
    assert "claude-haiku-5: HTTP 529" in message
    assert "one provider only (anthropic)" in message
    assert "Please try again" in message
    classification = classify_run_failure(caught.value)
    assert classification.cause is FailureCause.ALL_PROVIDERS_FAILED
    assert "claude-sonnet-5:HTTP 529" in (classification.detail or "")


def test_a_secret_in_a_leg_error_is_redacted_everywhere():
    secret = "sk-live" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    bearer = "Bearer " + "eyJhbGciOiJIUzI1NiJ9xyzABC123456"
    error = AllProvidersFailedError(
        [
            ("openai", RuntimeError(f"invalid key {secret}")),
            ("mistral", ConnectionError(f"proxy said Authorization: {bearer}")),
        ],
        tried=[("cfg-a", "gpt-6-astra"), ("cfg-b", "mistral-large")],
    )

    failure = platform_api.classify_platform_turn_failure(error)
    surfaces = [
        str(error),
        json.dumps(classify_run_failure(error).as_trace_metadata()),
        json.dumps(failure.body("c")),
        json.dumps(failure.frame()),
    ]
    for text in surfaces:
        assert secret not in text
        assert "eyJhbGciOiJIUzI1NiJ9xyzABC123456" not in text
    assert [leg["kind"] for leg in failure.body("c")["legs"]] == ["other", "connection"]


def test_a_leg_error_is_bounded():
    error = AllProvidersFailedError([("openai", RuntimeError("x " * 1000))])

    assert len(error.legs[0].error or "") <= 200
