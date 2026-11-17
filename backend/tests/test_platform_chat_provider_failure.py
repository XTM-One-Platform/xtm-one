"""A provider failure on a platform chat turn is an error status, never an answer.

The non-streaming platform chat path (``stream: false``, what an OpenCTI
playbook AI step calls) caught every exception of ``run_agent_chat`` and
answered HTTP 200 with an apology as ``content``: the caller parsed the apology
as the agent's output and never retried. A provider failure now answers 503
(429 for a rate limit, with ``Retry-After`` when the provider sent one) and a
machine-readable body; the streaming path emits the same ``error_type`` in a
``status: error`` frame.

A failover leg on the provider that just failed (the platform fallback model
owned by a config of the primary's provider) waits a jittered backoff first,
and an exhausted chain that held one provider says so.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import openai
import pytest
from anthropic._exceptions import OverloadedError
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.llm import router
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider, invalidate_provider_cache

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _overloaded() -> OverloadedError:
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = httpx.Response(529, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return OverloadedError(f"Error code: 529 - {body}", response=response, body=body)


def _rate_limited(retry_after: str | None = "7") -> openai.RateLimitError:
    body = {"error": {"message": "Rate limit reached", "type": "requests", "param": None, "code": None}}
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, json=body, headers=headers, request=httpx.Request("POST", _OPENAI_URL))
    return openai.RateLimitError(f"Error code: 429 - {body}", response=response, body=body["error"])


def _auth_rejected() -> openai.AuthenticationError:
    body = {"error": {"message": "Incorrect API key", "type": "invalid_request_error", "param": None, "code": None}}
    response = httpx.Response(401, json=body, request=httpx.Request("POST", _OPENAI_URL))
    return openai.AuthenticationError(f"Error code: 401 - {body}", response=response, body=body["error"])


# -- The answer a calling platform gets -----------------------------------------


def test_an_exhausted_chain_is_a_retryable_503_with_the_last_leg_status():
    failure = platform_api.classify_platform_turn_failure(
        AllProvidersFailedError([("anthropic", _overloaded()), ("anthropic", _overloaded())])
    )

    assert failure.status_code == 503
    assert failure.error_type == "provider_unavailable"
    assert failure.retryable is True
    assert failure.last_status == 529
    assert "one provider only (anthropic)" in failure.detail


def test_a_rate_limit_is_a_retryable_429_with_the_provider_retry_after():
    failure = platform_api.classify_platform_turn_failure(
        AllProvidersFailedError([("anthropic", _overloaded()), ("openai", _rate_limited("7"))])
    )

    assert (failure.status_code, failure.error_type, failure.retryable) == (429, "rate_limited", True)
    assert failure.retry_after == 7


def test_a_rejected_credential_is_not_retryable():
    failure = platform_api.classify_platform_turn_failure(AllProvidersFailedError([("openai", _auth_rejected())]))

    assert (failure.status_code, failure.error_type, failure.retryable) == (502, "provider_rejected", False)


def test_a_platform_bug_is_a_500_that_is_not_retryable():
    failure = platform_api.classify_platform_turn_failure(KeyError("agent_id"))

    assert (failure.status_code, failure.error_type, failure.retryable) == (500, "internal_error", False)


def test_a_bare_timeout_is_provider_weather():
    failure = platform_api.classify_platform_turn_failure(TimeoutError())

    assert (failure.status_code, failure.retryable) == (503, True)


def test_the_stream_frame_carries_the_same_error_type():
    failure = platform_api.classify_platform_turn_failure(AllProvidersFailedError([("anthropic", _overloaded())]))

    frame = failure.frame()
    legs = frame.pop("legs")
    assert frame == {
        "type": "status",
        "status": "error",
        "error_type": "provider_unavailable",
        "retryable": True,
        "last_status": 529,
    }
    assert [(leg["provider"], leg["kind"], leg["status"]) for leg in legs] == [("anthropic", "http_error", 529)]


# -- The non-streaming route ----------------------------------------------------


async def _send_failing(monkeypatch, exc: BaseException):
    stored: list = []
    conversations: list = []

    async def _resolve(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4(), name="Alpha")

    async def _store_message(_db, _conv_id, role, content, channel, metadata_json=None):
        stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json, role=role)

    async def _history(_db, _conv_id):
        return []

    async def _quota(_db, _user_id):
        return None

    async def _run_agent_chat(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run_agent_chat)

    class _DB:
        def add(self, obj):
            conversations.append(obj)

        async def flush(self):
            for obj in conversations:
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()

        async def commit(self):
            return None

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/platform/chat/messages", "headers": [], "query_string": b""}
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="transform this bundle", agent_slug="alpha", stream=False),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )
    return response, stored


@pytest.mark.asyncio
async def test_a_provider_outage_answers_503_not_a_200_apology(monkeypatch):
    response, stored = await _send_failing(
        monkeypatch, AllProvidersFailedError([("anthropic", _overloaded()), ("anthropic", _overloaded())])
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error_type"] == "provider_unavailable"
    assert body["retryable"] is True
    assert body["last_status"] == 529
    assert [leg["status"] for leg in body["legs"]] == [529, 529]
    assert "content" not in body
    # No apology stored as the agent's answer either.
    assert [role for role, _ in stored] == ["user"]


@pytest.mark.asyncio
async def test_a_rate_limit_answers_429_with_retry_after(monkeypatch):
    response, _ = await _send_failing(monkeypatch, AllProvidersFailedError([("openai", _rate_limited("12"))]))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    body = json.loads(response.body)
    assert body["error_type"] == "rate_limited"
    assert [(leg["provider"], leg["status"]) for leg in body["legs"]] == [("openai", 429)]


@pytest.mark.asyncio
async def test_a_rejected_credential_answers_502_with_its_legs(monkeypatch):
    response, _ = await _send_failing(monkeypatch, AllProvidersFailedError([("openai", _auth_rejected())]))

    assert response.status_code == 502
    assert [(leg["kind"], leg["status"]) for leg in json.loads(response.body)["legs"]] == [("http_error", 401)]


# -- Failover on the provider that just failed ----------------------------------


class _Provider(BaseLLMProvider):
    def __init__(self, name: str, default_model: str, error: BaseException | None = None):
        self.provider_name = name
        self.default_model = default_model
        self.error = error
        self.provider_config_id = str(uuid.uuid4())
        self.calls: list[str | None] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls.append(model or self.default_model)
        if self.error is not None:
            raise self.error
        return ChatResponse(content=f"answer from {self.provider_name}", model=model or "", usage={})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        self.calls.append(model or self.default_model)
        if self.error is not None:
            raise self.error
        yield f"answer from {self.provider_name}"

    async def chat_stream_with_tools(
        self,
        messages,
        tools=None,
        *,
        on_token=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        **kwargs,
    ):
        return await self.chat(messages, model=model)

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _fresh_router_state():
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


@pytest.fixture
def backoffs(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def _record():
        calls.append(1)

    monkeypatch.setattr(router, "_same_provider_backoff", _record)
    return calls


def _same_provider_chain(fallback: _Provider) -> FailoverLLMProvider:
    config = SimpleNamespace(id=uuid.uuid4(), provider=fallback.provider_name)
    return FailoverLLMProvider(
        primary=_Provider("anthropic", "claude-opus-5", _overloaded()),
        fallbacks=[],
        fallback_model="claude-sonnet-4-5",
        fallback_model_config=config,  # type: ignore[arg-type]
        prepared={str(config.id): fallback},
    )


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="transform this bundle")]


@pytest.mark.asyncio
async def test_a_second_leg_on_the_same_provider_backs_off_and_the_error_says_one_provider(backoffs):
    provider = _same_provider_chain(_Provider("anthropic", "claude-sonnet-4-5", _overloaded()))

    with pytest.raises(AllProvidersFailedError) as info:
        await provider.chat(_messages())

    assert backoffs == [1]
    assert info.value.single_provider is True
    assert "The failover chain held one provider only (anthropic)" in str(info.value)


@pytest.mark.asyncio
async def test_a_streaming_second_leg_on_the_same_provider_backs_off(backoffs):
    fallback = _Provider("anthropic", "claude-sonnet-4-5")
    provider = _same_provider_chain(fallback)

    chunks = [chunk async for chunk in provider.chat_stream(_messages())]

    assert chunks == ["answer from anthropic"]
    assert backoffs == [1]


@pytest.mark.asyncio
async def test_a_leg_on_another_provider_does_not_wait(backoffs):
    provider = FailoverLLMProvider(
        primary=_Provider("anthropic", "claude-opus-5", _overloaded()),
        fallbacks=[_Provider("gemini", "gemini-2.5-pro", _overloaded())],
    )

    with pytest.raises(AllProvidersFailedError) as info:
        await provider.chat(_messages())

    assert backoffs == []
    assert info.value.single_provider is False
    assert "one provider only" not in str(info.value)


def test_a_chain_of_one_leg_does_not_claim_a_single_provider_chain():
    error = AllProvidersFailedError([("anthropic", _overloaded())])

    assert error.single_provider is False
