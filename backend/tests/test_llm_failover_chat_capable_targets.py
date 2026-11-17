"""Failover never lands on a non-chat model, and says which model it used.

A PR-review sub-agent run failed with ``AllProvidersFailedError``: the primary
answered 529, and the OpenAI fallback answered "This is not a chat model and
thus not supported in the v1/chat/completions endpoint". Nothing in the logs
named the model the fallback had been called with.

Provider failures here are the SDKs' own exception classes, built the way their
clients build them from an HTTP answer.
"""

import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import openai
import pytest
from anthropic._exceptions import OverloadedError

from app.llm import router
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import (
    AllProvidersFailedError,
    FailoverLLMProvider,
    invalidate_provider_cache,
    is_non_chat_model_error,
)

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_STATUS_ERRORS: dict[int, type[openai.APIStatusError]] = {
    400: openai.BadRequestError,
    401: openai.AuthenticationError,
    404: openai.NotFoundError,
    429: openai.RateLimitError,
}


def _openai_error(
    status: int, message: str, *, param: str | None = None, code: str | None = None
) -> openai.APIStatusError:
    """What ``openai.AsyncOpenAI`` raises for an HTTP *status* answer carrying *message*.

    Same body, message and class dispatch as ``AsyncOpenAI._make_status_error``.
    """
    error = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    body = {"error": error}
    response = httpx.Response(status, json=body, request=httpx.Request("POST", _OPENAI_CHAT_URL))
    error_class = _OPENAI_STATUS_ERRORS.get(
        status, openai.InternalServerError if status >= 500 else openai.APIStatusError
    )
    return error_class(f"Error code: {status} - {body}", response=response, body=error)


def _anthropic_overloaded() -> OverloadedError:
    """What ``anthropic.AsyncAnthropic`` raises for a 529."""
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = httpx.Response(
        529, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return OverloadedError(f"Error code: 529 - {body}", response=response, body=body)


def _not_a_chat_model() -> openai.NotFoundError:
    """The refusal OpenAI answers for a non-chat model."""
    return _openai_error(
        404,
        "This is not a chat model and thus not supported in the v1/chat/completions "
        "endpoint. Did you mean to use v1/completions?",
        param="model",
    )


_REFUSALS = [
    pytest.param(_not_a_chat_model(), id="completion-only"),
    pytest.param(
        _openai_error(
            404,
            "This model is only supported in v1/responses and not in v1/chat/completions.",
            param="model",
        ),
        id="responses-only",
    ),
    pytest.param(
        _openai_error(
            400,
            "litellm.BadRequestError: OpenAIException - This model is only supported in "
            "v1/responses and not in v1/chat/completions.. Received Model "
            "Group=openai/gpt-5-codex\nAvailable Model Group Fallbacks=None",
            param="model",
            code="400",
        ),
        id="responses-only-relayed-by-litellm",
    ),
]

_OTHER_FAILURES = [
    pytest.param(
        _openai_error(
            400,
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead.",
            param="max_tokens",
            code="unsupported_parameter",
        ),
        id="unsupported-parameter",
    ),
    pytest.param(
        _openai_error(
            400,
            "'web_search_options' is not supported in the v1/chat/completions endpoint.",
            param="web_search_options",
        ),
        id="parameter-refused-by-the-chat-endpoint",
    ),
    pytest.param(
        _openai_error(
            400,
            "This model's maximum context length is 128000 tokens. However, your "
            "messages resulted in 131072 tokens. Please reduce the length of the messages.",
            param="messages",
            code="context_length_exceeded",
        ),
        id="context-length",
    ),
    pytest.param(
        _openai_error(
            404,
            "The model `gpt-9` does not exist or you do not have access to it.",
            code="model_not_found",
        ),
        id="unknown-model",
    ),
    pytest.param(_openai_error(404, "No endpoints found for openai/gpt-4o."), id="no-endpoint-for-the-model"),
    pytest.param(
        _openai_error(
            429,
            "Rate limit reached for gpt-4o in organization org-test on tokens per min "
            "(TPM): Limit 30000, Used 30000, Requested 1200.",
            code="rate_limit_exceeded",
        ),
        id="rate-limit",
    ),
    pytest.param(
        _openai_error(401, "Incorrect API key provided: sk-test.", code="invalid_api_key"),
        id="auth",
    ),
    pytest.param(_openai_error(503, "The server is overloaded or not ready yet."), id="overloaded-503"),
    pytest.param(_anthropic_overloaded(), id="overloaded-529"),
    pytest.param(openai.APITimeoutError(request=httpx.Request("POST", _OPENAI_CHAT_URL)), id="timeout"),
    pytest.param(RuntimeError("This is not a chat model"), id="refusal-text-without-status-or-param"),
]


class _Provider(BaseLLMProvider):
    """A provider that records its calls and either answers or raises."""

    def __init__(
        self,
        name: str,
        default_model: str | None,
        error: BaseException | None = None,
        *,
        config_id: str | None = None,
    ):
        self.provider_name = name
        self.default_model = default_model
        self.error = error
        # What ``build_provider`` stamps on an instance built from a config.
        self.provider_config_id = config_id
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
        self, messages, tools=None, *, on_token=None, model=None, temperature=0.7, max_tokens=4096, **kwargs,
    ):
        return await self.chat(messages, model=model)

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


def _config_id() -> str:
    return str(uuid.uuid4())


def _primary() -> _Provider:
    return _Provider("anthropic", "claude-opus-5", _anthropic_overloaded())


def _answering() -> _Provider:
    return _Provider("gemini", "gpt-4o")


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="review this PR")]


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


@pytest.fixture(autouse=True)
def _fresh_router_state():
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


def test_the_capability_rules_classify_the_models_these_tests_use():
    assert router.is_chat_capable_model("gpt-4o") is True
    assert router.is_chat_capable_model("gpt-3.5-turbo-instruct") is False
    assert router.is_chat_capable_model("text-embedding-3-small") is False
    # An id the catalog does not know fails open: only the endpoint can refuse it.
    assert router.is_chat_capable_model("brand-new-model-x") is True


# -- Declared capability --------------------------------------------------------


async def test_a_completion_only_default_model_is_never_a_failover_target():
    completion_only = _Provider("openai", "gpt-3.5-turbo-instruct")
    chat_capable = _answering()
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[completion_only, chat_capable])

    response = await provider.chat(_messages())

    assert response.content == "answer from gemini"
    assert completion_only.calls == []
    assert chat_capable.calls == ["gpt-4o"]


async def test_the_streaming_path_skips_a_non_chat_target_too():
    embedding = _Provider("openai", "text-embedding-3-small")
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[embedding, _answering()])

    chunks = await _collect(provider.chat_stream(_messages()))

    assert chunks == ["answer from gemini"]
    assert embedding.calls == []


async def test_the_stream_with_tools_path_skips_a_non_chat_target_too():
    completion_only = _Provider("openai", "gpt-3.5-turbo-instruct")
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[completion_only, _answering()])

    response = await provider.chat_stream_with_tools(_messages(), tools=None)

    assert response.content == "answer from gemini"
    assert completion_only.calls == []


def test_an_explicit_non_chat_fallback_model_is_not_a_target():
    owner = _Provider("openai", "gpt-4o")
    assert FailoverLLMProvider._can_chat(owner, "text-embedding-3-small") is False
    assert FailoverLLMProvider._can_chat(owner, "gpt-4o") is True


async def test_a_target_without_a_known_model_keeps_its_built_in_default():
    unknown = _Provider("mock_fallback", None)
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[unknown])

    response = await provider.chat(_messages())

    assert response.content == "answer from mock_fallback"


async def test_a_chain_left_with_no_target_says_what_failover_skipped():
    completion_only = _Provider("openai", "gpt-3.5-turbo-instruct")
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[completion_only])

    with pytest.raises(AllProvidersFailedError) as info:
        await provider.chat(_messages())

    assert completion_only.calls == []
    assert [name for name, _ in info.value.failures] == ["anthropic"]
    assert info.value.skipped == [("openai", "gpt-3.5-turbo-instruct")]
    assert "Failover skipped 'openai' (model=gpt-3.5-turbo-instruct): not a chat model." in str(info.value)


async def test_a_streaming_chain_left_with_no_target_says_it_too():
    provider = FailoverLLMProvider(
        primary=_primary(),
        fallbacks=[_Provider("openai", "gpt-3.5-turbo-instruct"), _Provider("mistral", "mistral-embed")],
    )

    with pytest.raises(AllProvidersFailedError) as info:
        await _collect(provider.chat_stream(_messages()))

    assert info.value.skipped == [("openai", "gpt-3.5-turbo-instruct"), ("mistral", "mistral-embed")]
    assert "(model=mistral-embed): not chat models." in str(info.value)


async def test_a_chain_that_tried_a_target_does_not_list_skipped_ones():
    provider = FailoverLLMProvider(
        primary=_primary(),
        fallbacks=[
            _Provider("openai", "gpt-3.5-turbo-instruct"),
            _Provider("gemini", "gemini-2.5-pro", _openai_error(503, "The server is overloaded or not ready yet.")),
        ],
    )

    with pytest.raises(AllProvidersFailedError) as info:
        await provider.chat(_messages())

    assert [name for name, _ in info.value.failures] == ["anthropic", "gemini"]
    assert info.value.skipped == []


def test_an_exhausted_chain_keeps_its_message_when_nothing_was_skipped():
    error = AllProvidersFailedError([("anthropic", RuntimeError("a")), ("openai", RuntimeError("b"))])

    assert str(error) == (
        "All configured AI providers failed (anthropic, openai): anthropic: a; openai: b. "
        "Please try again — if the issue persists, contact your administrator."
    )
    assert error.skipped == []


# -- Logs -----------------------------------------------------------------------


async def test_the_failover_log_names_the_config_and_the_model(caplog):
    config_id = _config_id()
    failing = _Provider(
        "openai", "gpt-4o", _openai_error(500, "The server had an error processing your request."), config_id=config_id
    )
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[failing, _Provider("gemini", "gemini-2.5-pro")])

    with caplog.at_level(logging.INFO, logger="app.llm.router"):
        await provider.chat(_messages())

    assert f"Failing over chat to 'openai' (config={config_id}, model=gpt-4o)" in caplog.text
    assert f"Fallback LLM provider 'openai' chat also failed (config={config_id}, model=gpt-4o)" in caplog.text
    assert "Failing over chat to 'gemini' (model=gemini-2.5-pro)" in caplog.text


async def test_the_streaming_failover_log_names_the_config_and_the_model_too(caplog):
    config_id = _config_id()
    failing = _Provider(
        "openai", "gpt-4o", _openai_error(500, "The server had an error processing your request."), config_id=config_id
    )
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[failing, _Provider("gemini", "gemini-2.5-pro")])

    with caplog.at_level(logging.INFO, logger="app.llm.router"):
        await _collect(provider.chat_stream(_messages()))

    assert f"Streaming failover to 'openai' (config={config_id}, model=gpt-4o)" in caplog.text
    assert f"Fallback LLM provider 'openai' stream also failed (config={config_id}, model=gpt-4o)" in caplog.text


def test_build_provider_stamps_the_config_id_on_the_instance():
    from app.core.security import encrypt_value

    config_id = uuid.uuid4()
    config = SimpleNamespace(
        id=config_id,
        provider="anthropic",
        api_key_enc=encrypt_value("sk-ant-test"),
        default_model="claude-sonnet-4-6",
        available_models=["claude-sonnet-4-6"],
        context_windows=None,
        max_output_tokens=None,
    )

    assert router.build_provider(config).provider_config_id == str(config_id)


# -- What the endpoint said -----------------------------------------------------


@pytest.mark.parametrize("error", _REFUSALS)
def test_is_non_chat_model_error_matches_an_endpoint_refusing_the_model(error):
    assert is_non_chat_model_error(error) is True


@pytest.mark.parametrize("error", _OTHER_FAILURES)
def test_is_non_chat_model_error_ignores_every_other_failure(error):
    assert is_non_chat_model_error(error) is False


async def test_a_config_whose_endpoint_refused_the_model_is_not_retried(caplog):
    config_id = _config_id()
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=config_id)
    answering = _answering()
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, answering])

    with caplog.at_level(logging.WARNING, logger="app.llm.router"):
        assert (await provider.chat(_messages())).content == "answer from gemini"
        assert (await provider.chat(_messages())).content == "answer from gemini"

    assert refusing.calls == ["brand-new-model-x"]
    assert answering.calls == ["gpt-4o", "gpt-4o"]
    refusal_log = (
        f"LLM provider 'openai' (config={config_id}, model=brand-new-model-x) refused a "
        "chat call because the model cannot chat; failover skips it for 15 minutes"
    )
    assert caplog.text.count(refusal_log) == 1


async def test_a_refusal_on_the_streaming_path_is_remembered_too():
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=_config_id())
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()])

    await _collect(provider.chat_stream(_messages()))
    await _collect(provider.chat_stream(_messages()))

    assert refusing.calls == ["brand-new-model-x"]


async def test_the_refusal_belongs_to_the_config_not_to_the_instance():
    # ``_get_cached_provider`` rebuilds a config's instance every
    # ``_PROVIDER_CACHE_TTL``: the rebuilt instance carries the same config id.
    config_id = _config_id()
    first = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=config_id)
    await FailoverLLMProvider(primary=_primary(), fallbacks=[first, _answering()]).chat(_messages())

    rebuilt = _Provider("openai", "brand-new-model-x", config_id=config_id)
    response = await FailoverLLMProvider(primary=_primary(), fallbacks=[rebuilt, _answering()]).chat(_messages())

    assert response.content == "answer from gemini"
    assert rebuilt.calls == []


async def test_one_config_refusing_a_model_does_not_disable_a_sibling_config():
    # Two configs of the same provider type can point at different endpoints.
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=_config_id())
    sibling = _Provider("openai", "brand-new-model-x", config_id=_config_id())
    await FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()]).chat(_messages())

    response = await FailoverLLMProvider(primary=_primary(), fallbacks=[sibling]).chat(_messages())

    assert response.content == "answer from openai"
    assert sibling.calls == ["brand-new-model-x"]


async def test_another_model_on_the_refusing_config_is_still_tried():
    # An admin moving the config to another model is not held back by the refusal.
    config_id = _config_id()
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=config_id)
    await FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()]).chat(_messages())

    edited = _Provider("openai", "gpt-4o", config_id=config_id)
    response = await FailoverLLMProvider(primary=_primary(), fallbacks=[edited]).chat(_messages())

    assert response.content == "answer from openai"


async def test_changing_the_llm_settings_re_arms_a_refused_model():
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=_config_id())
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()])

    await provider.chat(_messages())
    invalidate_provider_cache()
    await provider.chat(_messages())

    assert refusing.calls == ["brand-new-model-x", "brand-new-model-x"]


async def test_a_refusal_expires():
    config_id = _config_id()
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=config_id)
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()])
    await provider.chat(_messages())

    key = (config_id, "brand-new-model-x")
    remaining = router._refused_chat_targets[key] - time.monotonic()
    assert remaining == pytest.approx(router._REFUSED_CHAT_TARGET_TTL, abs=60)
    router._refused_chat_targets[key] = time.monotonic() - 1  # the TTL has elapsed
    await provider.chat(_messages())

    assert refusing.calls == ["brand-new-model-x", "brand-new-model-x"]


async def test_the_refusal_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(router, "_MAX_REFUSED_CHAT_TARGETS", 2)
    refusing = [
        _Provider("openai", f"brand-new-model-{index}", _not_a_chat_model(), config_id=_config_id())
        for index in range(3)
    ]
    for target in refusing:
        await FailoverLLMProvider(primary=_primary(), fallbacks=[target, _answering()]).chat(_messages())

    assert list(router._refused_chat_targets) == [
        (refusing[1].provider_config_id, "brand-new-model-1"),
        (refusing[2].provider_config_id, "brand-new-model-2"),
    ]


async def test_a_provider_built_without_a_config_is_not_remembered():
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model())
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()])

    await provider.chat(_messages())
    await provider.chat(_messages())

    assert refusing.calls == ["brand-new-model-x", "brand-new-model-x"]


async def test_a_learned_refusal_never_filters_the_primary():
    config_id = _config_id()
    refusing = _Provider("openai", "brand-new-model-x", _not_a_chat_model(), config_id=config_id)
    await FailoverLLMProvider(primary=_primary(), fallbacks=[refusing, _answering()]).chat(_messages())

    primary = _Provider("openai", "brand-new-model-x", config_id=config_id)
    response = await FailoverLLMProvider(primary=primary, fallbacks=[_answering()]).chat(_messages())

    assert response.content == "answer from openai"
    assert primary.calls == ["brand-new-model-x"]


@pytest.mark.parametrize("error", _OTHER_FAILURES)
async def test_an_ordinary_failure_never_takes_a_target_out(error):
    flaky = _Provider("openai", "gpt-4o", error, config_id=_config_id())
    provider = FailoverLLMProvider(primary=_primary(), fallbacks=[flaky, _Provider("gemini", "gemini-2.5-pro")])

    for _ in range(2):
        # A request-shaped failure (context length) stops the chain on the spot.
        with contextlib.suppress(AllProvidersFailedError):
            await provider.chat(_messages())

    assert flaky.calls == ["gpt-4o", "gpt-4o"]
    assert router._refused_chat_targets == {}
