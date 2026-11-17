from app.api.v1.llm_proxy import (
    _chat_model_mismatch_error_detail,
    _is_non_chat_model_error,
    _non_chat_model_http_exception,
    _supports_chat_completions,
)


# A throw-away exception class whose ``__class__.__name__`` matches the
# OpenAI / OpenAI-compatible SDK class that surfaces the "not a chat model"
# error. The tightened ``_is_non_chat_model_error`` predicate gates on the
# class name, so production-shaped exceptions must be NotFoundError; tests
# build a stand-in here to avoid importing the SDK just for the type.
class NotFoundError(Exception):
    pass


def test_is_non_chat_model_error_matches_openai_not_chat_message():
    exc = NotFoundError(
        "Error code: 404 - {'error': {'message': 'This is not a chat model and thus "
        "not supported in the v1/chat/completions endpoint. Did you mean to use "
        "v1/completions?'}}"
    )
    assert _is_non_chat_model_error(exc)


def test_is_non_chat_model_error_ignores_other_errors():
    # Plain Exception class is intentionally rejected — only NotFoundError
    # should ever come through this path.
    plain = Exception("Error code: 404 - {'error': {'message': 'Model not found'}}")
    assert not _is_non_chat_model_error(plain)
    # NotFoundError with an unrelated message must also be rejected.
    unrelated = NotFoundError(
        "Error code: 404 - {'error': {'message': 'Model not found'}}"
    )
    assert not _is_non_chat_model_error(unrelated)


def test_non_chat_model_http_exception_has_clear_actionable_detail():
    http_exc = _non_chat_model_http_exception("text-davinci-003")
    assert http_exc.status_code == 400
    assert "text-davinci-003" in str(http_exc.detail)
    assert "/v1/chat/completions" in str(http_exc.detail)
    assert "/v1/completions" in str(http_exc.detail)


def test_supports_chat_completions_accepts_chat_models() -> None:
    assert _supports_chat_completions("gpt-4o")
    assert _supports_chat_completions("claude-sonnet-4-20250514")
    assert _supports_chat_completions("gemini-2.5-pro")
    # Empty / whitespace-only / missing model strings are *not* the
    # proxy's job to validate here — they're handled at upstream
    # validation. The proactive guard intentionally defaults to True so
    # downstream errors surface as the user expects.
    assert _supports_chat_completions(None)
    assert _supports_chat_completions("")
    assert _supports_chat_completions("   ")


def test_supports_chat_completions_rejects_non_chat_models() -> None:
    assert not _supports_chat_completions("gpt-3.5-turbo-instruct")
    assert not _supports_chat_completions("text-embedding-3-large")
    assert not _supports_chat_completions("omni-moderation-latest")
    # The exact-match list catches legacy completion-only models that
    # don't share a prefix with the modern non-chat families.
    assert not _supports_chat_completions("babbage-002")
    assert not _supports_chat_completions("davinci-002")
    # Case-insensitive: callers may forward what they got from clients.
    assert not _supports_chat_completions("WHISPER-1")
    # Whitespace gets trimmed (matches normalisation elsewhere in the proxy).
    assert not _supports_chat_completions("  tts-1  ")


def test_supports_chat_completions_does_not_false_positive_on_substrings() -> None:
    """The original prefix-based design exists precisely so a hypothetical
    chat model name that *contains* a non-chat keyword (e.g. "embedding") is
    still allowed — only names that *start* with a known non-chat prefix
    are rejected. Locking this contract so a refactor doesn't slip into the
    keyword-substring shape."""
    # ``some-embedding-aware-chat`` contains the substring "embedding"
    # but does NOT start with ``text-embedding-`` — must pass through.
    assert _supports_chat_completions("some-embedding-aware-chat")
    # Same for "whisper" in the middle of the name.
    assert _supports_chat_completions("gpt-4o-whisper-fallback")


def test_supports_chat_completions_allows_instruct_chat_models() -> None:
    """Instruction-tuned chat models from non-OpenAI providers commonly end
    in ``-instruct`` and are routed through ``/v1/chat/completions``.

    There is no generic ``-instruct`` suffix rule on purpose: OpenAI's only
    completion-only ``-instruct`` model (``gpt-3.5-turbo-instruct``) is
    enumerated explicitly in ``_NON_CHAT_MODEL_EXACT``, so a future refactor
    that re-introduces a generic ``endswith("-instruct")`` would silently
    block valid Llama / Mistral / etc. instruction-tuned chat traffic. Pin
    that contract here.
    """
    # Bare ``-instruct`` shapes (no org prefix) — must pass through.
    assert _supports_chat_completions("llama-3.3-70b-instruct")
    assert _supports_chat_completions("mixtral-8x7b-instruct")
    assert _supports_chat_completions("qwen2.5-72b-instruct")
    # OpenRouter / LiteLLM ``org/model`` shapes.
    assert _supports_chat_completions("meta-llama/llama-3.3-70b-instruct")
    assert _supports_chat_completions("mistralai/mixtral-8x7b-instruct")
    assert _supports_chat_completions("qwen/qwen2.5-72b-instruct")
    # The one OpenAI completion-only ``-instruct`` model is still rejected
    # via the exact-match list, not via a generic suffix rule.
    assert not _supports_chat_completions("gpt-3.5-turbo-instruct")
    assert not _supports_chat_completions("openai/gpt-3.5-turbo-instruct")


def test_supports_chat_completions_normalizes_org_prefix() -> None:
    """OpenRouter / LiteLLM expose models as ``org/model``. The proactive
    guard must strip the org prefix before matching against
    ``_NON_CHAT_MODEL_PREFIXES`` / ``_NON_CHAT_MODEL_EXACT`` — otherwise
    ``openai/text-embedding-3-large`` would slip past the bare-prefix check
    and only fail at the upstream call.
    """
    # Prefix-matched non-chat families with the OpenAI org prefix.
    assert not _supports_chat_completions("openai/text-embedding-3-large")
    assert not _supports_chat_completions("openai/text-embedding-3-small")
    assert not _supports_chat_completions("openai/whisper-1")
    assert not _supports_chat_completions("openai/dall-e-3")
    assert not _supports_chat_completions("openai/tts-1")
    assert not _supports_chat_completions("openai/text-moderation-latest")
    assert not _supports_chat_completions("openai/omni-moderation-latest")
    # Exact-match legacy completion-only models with the org prefix.
    assert not _supports_chat_completions("openai/babbage-002")
    assert not _supports_chat_completions("openai/davinci-002")
    # Case-insensitive normalisation still applies after the org strip.
    assert not _supports_chat_completions("OpenAI/Whisper-1")
    # Multi-segment paths — only the trailing segment is matched.
    assert not _supports_chat_completions("vendor/sub/whisper-1")
    # Org-prefixed *chat* models still pass through.
    assert _supports_chat_completions("openai/gpt-4o")
    assert _supports_chat_completions("anthropic/claude-sonnet-4-20250514")


def test_is_non_chat_model_error_detects_openai_not_found_shape() -> None:
    err = NotFoundError(
        "This is not a chat model and thus not supported in the "
        "v1/chat/completions endpoint. Did you mean to use v1/completions?"
    )
    assert _is_non_chat_model_error(err)


def test_chat_model_mismatch_error_detail_contains_actionable_message() -> None:
    detail = _chat_model_mismatch_error_detail("gpt-3.5-turbo-instruct")
    assert detail["error"]["type"] == "invalid_request_error"
    assert detail["error"]["param"] == "model"
    assert "v1/chat/completions" in str(detail["error"]["message"])
    assert "v1/completions" in str(detail["error"]["message"])
