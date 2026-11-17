"""Tests for build_native_structured_output() FailoverLLMProvider unwrapping.

When an LLM is wrapped in a
`FailoverLLMProvider`, the native-structured-output detection used to
silently fall back to prompt-based SAP because the wrapper was not an
`OpenAICompatibleProvider` and its `provider_name` was always `""`.
"""

from types import SimpleNamespace

from app.services.structured_output import (
    build_native_structured_output,
    unwrap_failover_llm,
)

_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def test_native_format_selected_for_bare_openai_provider():
    llm = SimpleNamespace(provider_name="openai")
    result = build_native_structured_output(llm, _SCHEMA)
    assert isinstance(result, dict)
    assert result["type"] == "json_schema"


def test_native_format_selected_for_bare_anthropic_provider():
    llm = SimpleNamespace(provider_name="anthropic")
    result = build_native_structured_output(llm, _SCHEMA)
    assert isinstance(result, dict)


def test_native_format_selected_when_provider_wrapped_in_failover():
    # FailoverLLMProvider exposes the wrapped instance via `.primary`.
    primary = SimpleNamespace(provider_name="openai")
    failover = SimpleNamespace(provider_name="", primary=primary)
    result = build_native_structured_output(failover, _SCHEMA)
    assert isinstance(result, dict)
    assert result["type"] == "json_schema"


def test_native_format_selected_for_openai_compat_subclass_via_failover():
    from app.llm.openai_compat import OpenAICompatibleProvider

    class _FakeOpenRouter(OpenAICompatibleProvider):
        # Intentionally no `__init__` override: the real
        # `OpenAICompatibleProvider.__init__` requires API credentials
        # we don't have in tests, and inheriting without overriding
        # avoids the CodeQL "missing super().__init__()" warning that
        # any custom `__init__` would trip. Instances are constructed
        # via `object.__new__` below so the credential-requiring base
        # ctor never runs.
        provider_name = "openrouter"

    # `object.__new__` returns an `_FakeOpenRouter` instance without
    # ever calling the credential-requiring `__init__`. This is exactly
    # what we need to exercise the `isinstance(target, OpenAICompatibleProvider)`
    # branch in `build_native_structured_output`.
    primary = object.__new__(_FakeOpenRouter)
    failover = SimpleNamespace(provider_name="", primary=primary)
    result = build_native_structured_output(failover, _SCHEMA)
    assert isinstance(result, dict)
    assert result["type"] == "json_schema"


def test_returns_none_for_unknown_provider_even_through_failover():
    primary = SimpleNamespace(provider_name="some-future-provider")
    failover = SimpleNamespace(provider_name="", primary=primary)
    assert build_native_structured_output(failover, _SCHEMA) is None


# ---------------------------------------------------------------------------
# `unwrap_failover_llm` — shared helper
# ---------------------------------------------------------------------------


def test_unwrap_failover_llm_returns_primary_when_present():
    primary = SimpleNamespace(provider_name="anthropic")
    failover = SimpleNamespace(provider_name="", primary=primary)
    assert unwrap_failover_llm(failover) is primary


def test_unwrap_failover_llm_returns_llm_when_no_primary_attribute():
    bare = SimpleNamespace(provider_name="openai")
    assert unwrap_failover_llm(bare) is bare
