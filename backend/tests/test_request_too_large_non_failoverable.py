"""Tests for request-too-large (HTTP 413) non-failoverable handling and
is_chat_capable_model classification of legacy/gateway-prefixed completion-only
models.

Covers two root causes:
1. Anthropic HTTP 413 "request_too_large" errors were not recognised as
   non-failoverable, causing the system to burn through all fallback providers
   with the same oversized payload before surfacing the error.
2. Legacy OpenAI completion-only models (``text-davinci-003``) absent from the
   current LiteLLM catalog were classified as chat-capable, causing failover to
   land on them and 404 with "This is not a chat model".
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1.  _is_non_failoverable — HTTP 413 / request_too_large keywords
# ---------------------------------------------------------------------------


def test_anthropic_413_request_too_large_is_non_failoverable():
    """The Anthropic 413 error string must be caught as non-failoverable."""
    from app.llm.router import FailoverLLMProvider

    exc = Exception(
        "Error code: 413 - {'error': {'type': 'request_too_large', "
        "'message': 'Request exceeds the maximum size'}}"
    )
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


def test_request_too_large_keyword_is_non_failoverable():
    """The ``request_too_large`` keyword alone must trigger non-failoverable."""
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("request_too_large")
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


def test_request_exceeds_maximum_size_is_non_failoverable():
    """The ``request exceeds the maximum size`` phrase must trigger non-failoverable."""
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("Request exceeds the maximum size")
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


def test_content_size_limit_still_non_failoverable():
    """Pre-existing ``content size limit`` keyword must still be caught."""
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("content size limit exceeded")
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


def test_generic_network_error_remains_failoverable():
    """Transient network errors must NOT be treated as non-failoverable."""
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("connection reset by peer")
    assert FailoverLLMProvider._is_non_failoverable(exc) is False


def test_500_internal_server_error_remains_failoverable():
    """Provider-side 500 errors should still trigger failover."""
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("Error code: 500 - internal server error")
    assert FailoverLLMProvider._is_non_failoverable(exc) is False


# ---------------------------------------------------------------------------
# 2.  is_chat_capable_model — legacy completion-only models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "text-davinci-003",
        "text-davinci-002",
        "text-davinci-001",
        "text-curie-001",
        "text-babbage-001",
        "text-ada-001",
        "code-davinci-002",
        "code-cushman-001",
    ],
)
def test_legacy_completion_models_not_chat_capable(model_id: str):
    """Legacy OpenAI completion-only models must return False from
    is_chat_capable_model, even though they are absent from the current
    LiteLLM catalog."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/text-davinci-003",
        "azure/text-davinci-003",
        "openrouter/text-davinci-003",
    ],
)
def test_gateway_prefixed_legacy_completion_models_not_chat_capable(model_id: str):
    """Gateway-prefixed legacy completion-only models must also return False."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is False


# ---------------------------------------------------------------------------
# 3.  is_chat_capable_model — gateway-prefixed catalog completion-only models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-3.5-turbo-instruct",
        "gpt-3.5-turbo-instruct-0914",
        "davinci-002",
        "babbage-002",
    ],
)
def test_catalog_completion_only_models_not_chat_capable(model_id: str):
    """Models in the LiteLLM catalog with mode=completion must return False."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5.2-pro",
        "gpt-5.5-pro",
        "o3-pro",
        "o1-pro",
        "gpt-5-codex",
        "gpt-5.1-codex-max",
        "o3-deep-research",
    ],
)
def test_catalog_responses_only_models_not_chat_capable(model_id: str):
    """OpenAI models the catalog lists with mode=responses answer
    ``/v1/chat/completions`` with a 404 "This is not a chat model", so they are
    no chat default and no failover target."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is False


@pytest.mark.parametrize("model_id", ["openai/gpt-5.2-pro", "openai/gpt-5-codex"])
def test_gateway_prefixed_responses_only_models_stay_chat_capable(model_id: str):
    """OpenRouter serves these through its own chat surface."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is True


def test_translating_proxy_gateway_remains_chat_capable():
    """Gateway-prefixed completion-only models served through a translating proxy
    (e.g. Vercel AI Gateway) remain chat-capable because the proxy re-exposes them
    as chat endpoints.  The baseline id itself stays excluded."""
    from app.llm.router import is_chat_capable_model

    # Bare id excluded (completion-only in catalog).
    assert is_chat_capable_model("gpt-3.5-turbo-instruct") is False
    # Translating-proxy alias remains True — gateway makes it chat-capable.
    assert (
        is_chat_capable_model("vercel_ai_gateway/openai/gpt-3.5-turbo-instruct")
        is True
    )


@pytest.mark.parametrize(
    "model_id",
    [
        # Azure catalog aliases with mode=completion. A prefixed id only occurs
        # when routing through a translating gateway (LiteLLM prompt-adapts
        # completion-mode models to its chat surface), so it stays selectable.
        "azure/gpt-3.5-turbo-instruct-0914",
        "azure/gpt-35-turbo-instruct",
        # Ollama catalog entries with mode=completion: the same basename chats
        # fine on Ollama's native chat API / through LiteLLM.
        "ollama/mistral",
        "ollama/codellama",
    ],
)
def test_gateway_prefixed_catalog_completion_entries_stay_chat_capable(model_id: str):
    """PINNED DESIGN DECISION: catalog
    entries with mode=completion/realtime/responses are excluded by EXACT unprefixed key
    only. Gateway-prefixed completion entries stay chat-capable because the
    prefix implies a translating gateway that can serve them through a chat
    interface — excluding them would break working setups like ``ollama/mistral``
    via a LiteLLM proxy. If this test starts failing, the exact-unprefixed
    matching contract in ``_catalog_chat_classification`` changed."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is True


# ---------------------------------------------------------------------------
# 4.  is_chat_capable_model — genuine chat models still pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-4o",
        "claude-sonnet-5",
        "claude-3-5-haiku-20241022",
        "gemini-1.5-pro",
        "mistral-large-latest",
        "gpt-4o-mini",
        "anthropic/claude-3-5-sonnet",
        "openai/gpt-4o",
        "gpt-5.2",
        "gpt-5.6-sol",
        "o3",
    ],
)
def test_genuine_chat_models_are_chat_capable(model_id: str):
    """Genuine chat models must not be incorrectly classified as non-chat."""
    from app.llm.router import is_chat_capable_model

    assert is_chat_capable_model(model_id) is True


# ---------------------------------------------------------------------------
# 5.  _friendly_error_message — 413 surfaces helpful context-size message
# ---------------------------------------------------------------------------


def test_friendly_error_message_for_413_request_too_large():
    """A raw 413 / request_too_large error must map to the conversation-too-large
    message, not the generic 'all providers failed' message."""
    from app.api.v1.chat import _friendly_error_message

    exc = Exception(
        "Error code: 413 - {'error': {'type': 'request_too_large', "
        "'message': 'Request exceeds the maximum size'}}"
    )
    msg = _friendly_error_message(exc)
    assert "too large" in msg.lower()
    # Must mention starting a new conversation, not just "attachments"
    assert "new conversation" in msg.lower()


def test_friendly_error_message_all_size_failures_gives_context_hint():
    """AllProvidersFailedError where all failures were 413 must hint at
    conversation size rather than showing the generic provider-failure message."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError

    failures = [
        (
            "anthropic",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
        (
            "openai",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
    ]
    exc = AllProvidersFailedError(failures)
    msg = _friendly_error_message(exc)
    assert "too large" in msg.lower()
    assert "new conversation" in msg.lower()
    assert "administrator" not in msg.lower()


def test_friendly_error_message_mixed_failures_gives_generic_message():
    """AllProvidersFailedError whose LAST failure is not a size error must show
    the generic admin-contact message — the chain was not stopped by a 413."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError

    failures = [
        (
            "anthropic",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
        ("openai", Exception("Error code: 500 - internal server error")),
    ]
    exc = AllProvidersFailedError(failures)
    msg = _friendly_error_message(exc)
    assert "administrator" in msg.lower()


def test_friendly_error_message_transient_then_413_gives_size_hint():
    """The realistic short-circuit shape: the primary fails transiently
    (which is why failover started at all), then a fallback hits the 413 that
    stops the chain.  The surfaced message must be the size hint — the LAST
    failure is what ended the chain, and it reproduces on every provider."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError

    failures = [
        ("anthropic", Exception("Error code: 529 - overloaded")),
        (
            "openai",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
    ]
    exc = AllProvidersFailedError(failures)
    msg = _friendly_error_message(exc)
    assert "too large" in msg.lower()
    assert "new conversation" in msg.lower()
    assert "administrator" not in msg.lower()


# ---------------------------------------------------------------------------
# 6.  Generic gateway payload-size keywords (nginx / proxy 413 variants)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 413 - Payload Too Large",
        "413 Request Entity Too Large (nginx)",
        # Gemini's oversized-payload phrasing (google-genai ClientError, a 400
        # INVALID_ARGUMENT): the classification must compose across providers,
        # not just Anthropic/OpenAI-compatible phrasings.
        "400 INVALID_ARGUMENT. Request payload size exceeds the limit: 20971520 bytes.",
    ],
)
def test_gateway_payload_size_errors_are_non_failoverable(message: str):
    """Generic gateway 413 phrasings (self-hosted proxies in front of
    OpenAI-compatible endpoints) and Gemini's payload-size phrasing must also
    be caught as non-failoverable — the keyword set is shared with the chat
    error mapper so classification and user messaging stay in lockstep."""
    from app.llm.router import FailoverLLMProvider

    assert FailoverLLMProvider._is_non_failoverable(Exception(message)) is True


def test_size_keywords_shared_between_router_and_chat_mapper():
    """Every keyword the failover classifier treats as a size error must map to
    the request-too-large user message in the chat error mapper (no drift)."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import REQUEST_TOO_LARGE_KEYWORDS

    for kw in REQUEST_TOO_LARGE_KEYWORDS:
        msg = _friendly_error_message(Exception(kw))
        assert "too large" in msg.lower(), kw
        assert "new conversation" in msg.lower(), kw


# ---------------------------------------------------------------------------
# 7.  Mid-chain short-circuit — a 413 on a FALLBACK stops the rest of the chain
# ---------------------------------------------------------------------------


class _FailingProvider:
    """Minimal provider stub whose chat/stream calls raise a fixed error."""

    def __init__(self, name: str, error: Exception):
        self.provider_name = name
        self._error = error
        self.calls = 0

    async def chat(self, messages, model=None, **kwargs):
        self.calls += 1
        raise self._error

    async def chat_stream(self, messages, model=None, **kwargs):
        self.calls += 1
        if False:  # pragma: no cover — makes this an async generator
            yield ""
        raise self._error


class _RecordingProvider:
    """Minimal provider stub that records whether it was ever called."""

    def __init__(self, name: str):
        self.provider_name = name
        self.calls = 0

    async def chat(self, messages, model=None, **kwargs):
        self.calls += 1
        return object()

    async def chat_stream(self, messages, model=None, **kwargs):
        self.calls += 1
        yield "ok"


def _make_failover(primary, fallbacks):
    from app.llm.router import FailoverLLMProvider

    # Bypass __init__ attribute mirroring (the stubs don't implement the full
    # BaseLLMProvider surface) — install only what the failover paths read.
    provider = FailoverLLMProvider.__new__(FailoverLLMProvider)
    provider.primary = primary
    provider._fallbacks = fallbacks
    provider._fallback_configs = None
    provider._fallback_model = None
    provider._fallback_model_config = None
    provider._decline_routes = {}
    provider.provider_name = primary.provider_name
    return provider


@pytest.mark.asyncio
async def test_fallback_413_short_circuits_remaining_chain():
    """When the primary fails transiently and the FIRST fallback hits a 413,
    the remaining fallbacks must NOT be tried — the same oversized payload
    would reproduce on every one of them."""
    from app.llm.router import AllProvidersFailedError

    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    fb_413 = _FailingProvider(
        "openai",
        RuntimeError(
            "Error code: 413 - {'error': {'type': 'request_too_large', "
            "'message': 'Request exceeds the maximum size'}}"
        ),
    )
    fb_untouched = _RecordingProvider("gemini")
    provider = _make_failover(primary, [fb_413, fb_untouched])

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await provider._failover_call("chat", [], model="claude-sonnet-5")

    assert fb_413.calls == 1
    assert fb_untouched.calls == 0
    assert [name for name, _ in excinfo.value.failures] == ["anthropic", "openai"]


@pytest.mark.asyncio
async def test_fallback_413_short_circuits_remaining_chain_streaming():
    """Same mid-chain short-circuit for the ``chat_stream`` failover path."""
    from app.llm.router import AllProvidersFailedError

    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    fb_413 = _FailingProvider(
        "openai",
        RuntimeError(
            "Error code: 413 - {'error': {'type': 'request_too_large', "
            "'message': 'Request exceeds the maximum size'}}"
        ),
    )
    fb_untouched = _RecordingProvider("gemini")
    provider = _make_failover(primary, [fb_413, fb_untouched])

    with pytest.raises(AllProvidersFailedError):
        async for _ in provider.chat_stream([], model="claude-sonnet-5"):
            pass

    assert fb_413.calls == 1
    assert fb_untouched.calls == 0


@pytest.mark.asyncio
async def test_fallback_transient_error_still_continues_chain():
    """A transient fallback failure must keep walking the chain — the mid-chain
    short-circuit only applies to request-caused (non-failoverable) errors."""
    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    fb_transient = _FailingProvider("openai", RuntimeError("connection reset by peer"))
    fb_recovers = _RecordingProvider("gemini")
    provider = _make_failover(primary, [fb_transient, fb_recovers])

    await provider._failover_call("chat", [], model="claude-sonnet-5")

    assert fb_transient.calls == 1
    assert fb_recovers.calls == 1


# ---------------------------------------------------------------------------
# 8.  REQUEST_TOO_LARGE_STREAMING_MESSAGE — constant exported from router.py
# ---------------------------------------------------------------------------


def test_request_too_large_streaming_message_exported():
    """REQUEST_TOO_LARGE_STREAMING_MESSAGE must be importable from router.py."""
    from app.llm.router import REQUEST_TOO_LARGE_STREAMING_MESSAGE

    assert isinstance(REQUEST_TOO_LARGE_STREAMING_MESSAGE, str)
    assert len(REQUEST_TOO_LARGE_STREAMING_MESSAGE) > 0
    # Must mention starting a new conversation so users know the action to take.
    assert "new conversation" in REQUEST_TOO_LARGE_STREAMING_MESSAGE.lower()


def test_streaming_message_and_error_frame_message_are_consistent():
    """The streaming path and the WebSocket error-frame path must give equivalent
    guidance — both say the conversation is too large and suggest a new one."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import REQUEST_TOO_LARGE_STREAMING_MESSAGE

    exc = Exception(
        "Error code: 413 - {'error': {'type': 'request_too_large', "
        "'message': 'Request exceeds the maximum size'}}"
    )
    error_frame_msg = _friendly_error_message(exc)

    # Both messages must mention the same actionable guidance.
    assert "too large" in error_frame_msg.lower()
    assert "too large" in REQUEST_TOO_LARGE_STREAMING_MESSAGE.lower()
    assert "new conversation" in error_frame_msg.lower()
    assert "new conversation" in REQUEST_TOO_LARGE_STREAMING_MESSAGE.lower()


# ---------------------------------------------------------------------------
# 9.  is_request_too_large_error — shared graceful-handling predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_message",
    [
        # Anthropic native format (the production error from the issue report).
        "Error code: 413 - {'error': {'type': 'request_too_large', 'message': 'Request exceeds the maximum size'}}",
        # Generic gateway phrasing.
        "Error code: 413 - Payload Too Large",
        # Gemini INVALID_ARGUMENT phrasing.
        "400 INVALID_ARGUMENT. Request payload size exceeds the limit: 20971520 bytes.",
        # OpenAI-style content_size_limit.
        "content size limit exceeded",
    ],
)
def test_predicate_matches_raw_413_errors(exc_message: str):
    """Every exception string that triggers the FailoverLLMProvider
    non-failoverable classification must ALSO be detected by the shared
    graceful-handling predicate used by agent_chat / general_chat /
    public_chat / assignment_engine — same keyword set, no drift."""
    from app.llm.router import FailoverLLMProvider, is_request_too_large_error

    exc = Exception(exc_message)
    assert FailoverLLMProvider._is_non_failoverable(exc) is True
    assert is_request_too_large_error(exc) is True


def test_predicate_matches_wrapped_chain_ending_on_413():
    """The realistic short-circuit shape: the primary fails transiently (which
    is why failover started at all), then a fallback hits the 413 that stops
    the chain.  The surfaced exception is an ``AllProvidersFailedError`` whose
    ``str()`` contains NONE of the size keywords — the predicate must unwrap
    the LAST failure, exactly like ``_friendly_error_message``."""
    from app.llm.router import AllProvidersFailedError, is_request_too_large_error

    failures = [
        ("anthropic", Exception("Error code: 529 - overloaded")),
        (
            "openai",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
    ]
    exc = AllProvidersFailedError(failures)
    # Sanity: the wrapper's own message carries no size keyword — matching on
    # str(exc) alone would silently miss this shape.
    assert "too large" not in str(exc).lower()
    assert is_request_too_large_error(exc) is True


def test_predicate_rejects_wrapped_chain_ending_on_transient_error():
    """A chain that ended on a NON-size error (e.g. 500) must not be treated
    as request-too-large, even when an earlier failure in the chain was a 413
    — the last failure is what stopped the chain."""
    from app.llm.router import AllProvidersFailedError, is_request_too_large_error

    failures = [
        (
            "anthropic",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
        ("openai", Exception("Error code: 500 - internal server error")),
    ]
    exc = AllProvidersFailedError(failures)
    assert is_request_too_large_error(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("connection reset by peer"),
        RuntimeError("Error code: 500 - internal server error"),
        RuntimeError("Error code: 429 - rate limit exceeded"),
    ],
)
def test_predicate_rejects_non_size_errors(exc: Exception):
    """Transient / unrelated provider errors must not be classified as 413."""
    from app.llm.router import is_request_too_large_error

    assert is_request_too_large_error(exc) is False


def test_predicate_handles_empty_failure_list():
    """An ``AllProvidersFailedError`` with no failures must not crash the
    predicate (defensive: the constructor is public API)."""
    from app.llm.router import AllProvidersFailedError, is_request_too_large_error

    assert is_request_too_large_error(AllProvidersFailedError([])) is False


def test_friendly_error_message_uses_shared_predicate_for_wrapped_413():
    """The WebSocket error mapper and the graceful streaming handlers share
    the same predicate — a wrapped chain ending on a 413 must map to the
    size message on both surfaces."""
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError

    failures = [
        ("anthropic", Exception("Error code: 529 - overloaded")),
        (
            "openai",
            Exception(
                "Error code: 413 - {'error': {'type': 'request_too_large', "
                "'message': 'Request exceeds the maximum size'}}"
            ),
        ),
    ]
    msg = _friendly_error_message(AllProvidersFailedError(failures))
    assert "too large" in msg.lower()
    assert "new conversation" in msg.lower()


# ---------------------------------------------------------------------------
# AI Gateway: a body over 10 MiB is a 413 ``request_too_large``
# ---------------------------------------------------------------------------


def _gateway_error(status: int, code: str) -> Exception:
    import httpx
    import openai

    # A message with no size keyword: the status alone must decide.
    message = "the request body is over the 10 MiB limit"
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body={"message": message, "code": code})


def test_gateway_413_is_request_too_large():
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError, FailoverLLMProvider, is_request_too_large_error
    from app.services.failure_causes import FailureCause, FailureDetermination, classify_run_failure

    exc = _gateway_error(413, "request_too_large")
    assert is_request_too_large_error(exc) is True
    assert FailoverLLMProvider._is_non_failoverable(exc) is True
    chain = AllProvidersFailedError([("openai", Exception("Error code: 529 - overloaded")), ("gateway", exc)])
    assert is_request_too_large_error(chain) is True
    for raised in (exc, chain):
        msg = _friendly_error_message(raised).lower()
        assert "too large" in msg
        assert "new conversation" in msg
    result = classify_run_failure(exc)
    assert result.cause is FailureCause.REQUEST_TOO_LARGE
    assert result.determination is FailureDetermination.STRUCTURAL


def test_gateway_400_invalid_request_is_not_request_too_large():
    """A body cut off by the client stays a 400: not a size error."""
    from app.llm.router import is_request_too_large_error

    assert is_request_too_large_error(_gateway_error(400, "invalid_request")) is False


# The gateway's 10 MiB cap is its own, below some providers' (Anthropic: 32 MB):
# its 413 falls over, and a chain that ends on it still reads as too large.


@pytest.mark.asyncio
async def test_gateway_413_falls_over_to_the_next_provider():
    primary = _FailingProvider("filigran_gateway", _gateway_error(413, "request_too_large"))
    fallback = _RecordingProvider("anthropic")
    provider = _make_failover(primary, [fallback])

    await provider._failover_call("chat", [], model="claude-sonnet-5")
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_gateway_413_falls_over_to_the_next_provider_streaming():
    primary = _FailingProvider("filigran_gateway", _gateway_error(413, "request_too_large"))
    fallback = _RecordingProvider("anthropic")
    provider = _make_failover(primary, [fallback])

    chunks = [chunk async for chunk in provider.chat_stream([], model="claude-sonnet-5")]
    assert chunks == ["ok"]
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_gateway_413_mid_chain_falls_over():
    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    gateway = _FailingProvider("filigran_gateway", _gateway_error(413, "request_too_large"))
    last = _RecordingProvider("gemini")
    provider = _make_failover(primary, [gateway, last])

    await provider._failover_call("chat", [], model="claude-sonnet-5")
    assert last.calls == 1


@pytest.mark.asyncio
async def test_chain_ending_on_gateway_413_is_request_too_large():
    from app.api.v1.chat import _friendly_error_message
    from app.llm.router import AllProvidersFailedError, is_request_too_large_error

    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    gateway = _FailingProvider("filigran_gateway", _gateway_error(413, "request_too_large"))
    provider = _make_failover(primary, [gateway])

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await provider._failover_call("chat", [], model="claude-sonnet-5")
    assert is_request_too_large_error(excinfo.value) is True
    assert "too large" in _friendly_error_message(excinfo.value).lower()


@pytest.mark.asyncio
async def test_lone_gateway_413_is_request_too_large():
    from app.llm.router import is_request_too_large_error

    provider = _make_failover(_FailingProvider("filigran_gateway", _gateway_error(413, "request_too_large")), [])

    with pytest.raises(Exception) as excinfo:
        await provider._failover_call("chat", [], model="claude-sonnet-5")
    assert is_request_too_large_error(excinfo.value) is True


@pytest.mark.asyncio
async def test_provider_413_still_short_circuits_remaining_chain():
    from app.llm.router import AllProvidersFailedError

    primary = _FailingProvider("anthropic", RuntimeError("Error code: 529 - overloaded"))
    openai_413 = _FailingProvider("openai", _gateway_error(413, "request_too_large"))
    untouched = _RecordingProvider("gemini")
    provider = _make_failover(primary, [openai_413, untouched])

    with pytest.raises(AllProvidersFailedError):
        await provider._failover_call("chat", [], model="claude-sonnet-5")
    assert untouched.calls == 0


def test_httpx_413_without_keyword_is_request_too_large():
    """The status read off ``.response`` counts too (httpx-based providers)."""
    import httpx

    from app.llm.router import is_request_too_large_error

    request = httpx.Request("POST", "https://provider.example/v1/chat")
    response = httpx.Response(413, request=request)
    exc = httpx.HTTPStatusError("the body is over the limit", request=request, response=response)
    assert is_request_too_large_error(exc) is True
