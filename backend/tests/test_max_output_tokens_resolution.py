"""Per-model max-output-token resolution across all providers.

Regression coverage for the failure mode where a model released *after* the
bundled metadata snapshot (exercised below with the not-yet-existing dated
variant ``claude-opus-4-8-20260901``) silently regressed to the stale 8_192
provider class default - which, combined with reasoning effort
(whose thinking tokens count against ``max_tokens`` on Anthropic *and* Gemini),
starved the visible answer and any tool call, degrading the agent into "narrate
the plan, never execute it".

The fix centralises resolution in :mod:`app.llm.model_limits` with the chain
admin override -> metadata snapshot -> model-family floor -> provider flat
default -> hard fallback, shared by the agentic loop and the failover clamp.
"""

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.model_limits import (
    _FALLBACK_MAX_OUTPUT_TOKENS,
    resolve_effective_max_output_tokens,
    resolve_family_max_output_tokens,
    resolve_snapshot_max_output_tokens,
)
from app.llm.openai_provider import OpenAIProvider
from app.llm.router import FailoverLLMProvider
from app.tools.agent_loop import _resolve_effective_max_tokens


# --- Tier 3: model-family floors (forward-looking safety net) ----------------
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Anthropic (current generations only; legacy claude-3-* excluded).
        # The Claude 5 generation supports 128k output; 4.x floors stay 64k.
        ("claude-opus-5", 128_000),
        ("claude-opus-5-20260524", 128_000),
        ("claude-sonnet-5", 128_000),
        ("claude-opus-4-8", 64_000),
        ("claude-opus-4-8-20260901", 64_000),
        ("claude-sonnet-4-6", 64_000),
        ("claude-haiku-4-5", 64_000),
        ("claude-fable-5", 128_000),
        ("claude-mythos-5-1", 128_000),
        ("claude-mythos-preview", 128_000),
        ("anthropic.claude-mythos-5-1", 128_000),
        ("us.anthropic.claude-mythos-5-1-v1:0", 128_000),
        ("global.anthropic.claude-mythos-5-1-v1:0", 128_000),
        # Every Bedrock geographic inference-profile prefix, not only us / eu /
        # apac: Japan, Australia and GovCloud ids missed every family floor.
        ("jp.anthropic.claude-mythos-5-1-v1:0", 128_000),
        ("au.anthropic.claude-mythos-5-1-v1:0", 128_000),
        ("us-gov.anthropic.claude-sonnet-4-6-v1:0", 64_000),
        ("openrouter/anthropic/claude-mythos-5-1", 128_000),
        ("claude-mythos-5-1@20260901", 128_000),
        ("claude-mythos", None),
        # OpenAI families.
        ("gpt-6-astra", 128_000),
        ("gpt-5.5", 128_000),
        ("gpt-5", 128_000),
        ("gpt-4.1", 32_768),
        ("gpt-4o", 16_384),
        ("gpt-4o-mini", 16_384),
        ("o3", 100_000),
        ("o4-mini", 100_000),
        # Gemini (combined thinking+output budget).
        ("gemini-2.5-pro", 65_536),
        ("gemini-3-flash", 65_536),
        # Mistral: no separate output cap, so what fits beside a prompt
        # compaction lets reach 80% of the window.
        ("mistral-large-latest", 16_384),
        ("mistral-small-2603", 16_384),
        ("ministral-8b-latest", 16_384),
        ("codestral-latest", 16_384),
        ("devstral-medium-latest", 16_384),
        ("magistral-medium-latest", 8_000),
        ("mistral/mistral-medium-latest", 16_384),
        # Aggregator / cross-region prefixes normalise to the bare family id.
        ("anthropic/claude-opus-4-8", 64_000),
        ("us.anthropic.claude-opus-4-8-v1:0", 64_000),
        ("openai/gpt-5.5", 128_000),
        ("google/gemini-3-pro", 65_536),
        # Legacy / unknown names must NOT match (avoids over-requesting a 400).
        ("claude-3-opus-20240229", None),
        ("claude-3-5-sonnet-20241022", None),
        ("claude-3-5-haiku", None),
        ("some-unknown-model", None),
        (None, None),
    ],
)
def test_family_floor(model, expected):
    assert resolve_family_max_output_tokens(model) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("mistral-large-latest", 256_000),
        ("mistral-large-2512", 256_000),
        ("mistral-medium-latest", 256_000),
        ("mistral-medium-3-5", 256_000),
        ("mistral-small-latest", 256_000),
        ("mistral-small-2603", 256_000),
        ("codestral-latest", 256_000),
        # Earlier dated releases and the smaller families keep the floor
        # every current window clears.
        ("mistral-large-2411", 128_000),
        ("mistral-medium-2508", 128_000),
        ("mistral-small-2506", 128_000),
        ("ministral-8b-latest", 128_000),
        ("devstral-small-latest", 128_000),
        ("magistral-medium-latest", 40_000),
        ("mistral/mistral-large-latest", 256_000),
    ],
)
def test_mistral_family_context_window(model, expected):
    from app.llm.model_limits import resolve_family_context_window

    assert resolve_family_context_window(model) == expected


def test_mistral_provider_floors_fit_its_smallest_window():
    """An unlisted Mistral model gets an output that still fits beside the
    prompt compaction lets grow to 80% of the window (prompt + max_tokens
    must fit it)."""
    from app.llm.mistral_provider import MistralProvider

    assert MistralProvider.max_output_tokens <= MistralProvider.context_window * 0.2


@pytest.mark.parametrize(
    "model",
    [
        "mistral-large-latest",
        "mistral-large-2411",
        "mistral-medium-latest",
        "mistral-small-2506",
        "ministral-3b-latest",
        "magistral-medium-latest",
        "magistral-small-latest",
        "codestral-latest",
        "devstral-small-latest",
        "pixtral-large-latest",
        "mistral-unreleased-model",
    ],
)
def test_mistral_output_fits_beside_a_full_prompt(model):
    """What the Mistral provider resolves for a model must still fit its window
    once compaction let the prompt reach 80% of it."""
    from app.llm.mistral_provider import MistralProvider
    from app.llm.model_limits import resolve_effective_context_window, resolve_provider_max_output_tokens

    provider = MistralProvider(api_key="test-key")
    window = resolve_effective_context_window(
        model=model, overrides=None, provider_default=provider.context_window
    )

    assert resolve_provider_max_output_tokens(provider, model) <= window * 0.2


# --- The ceiling a provider applies, and the retry budget capped at it -------
def test_provider_ceiling_prefers_the_admin_override():
    from app.llm.model_limits import resolve_provider_max_output_tokens

    provider = AnthropicProvider(api_key="test-key")
    provider.max_output_tokens_overrides = {"claude-mythos-5": 5_000}

    assert resolve_provider_max_output_tokens(provider, "claude-mythos-5") == 5_000
    assert resolve_provider_max_output_tokens(provider, "claude-mythos-5-1") == 128_000


def test_provider_ceiling_is_unknown_without_any_source():
    from app.llm.model_limits import resolve_provider_max_output_tokens

    assert resolve_provider_max_output_tokens(object(), None) is None
    assert resolve_provider_max_output_tokens(object(), "some-unknown-model") is None


@pytest.mark.parametrize(
    ("overrides", "budget", "expected"),
    [
        # No known ceiling: twice the budget.
        (None, 2_000, 4_000),
        # The ceiling caps the doubled budget - over it is an HTTP 400.
        ({"m": 3_000}, 2_000, 3_000),
        # A ceiling under the first budget never lowers it.
        ({"m": 1_000}, 2_000, 2_000),
        ({"m": 64_000}, 1_500, 3_000),
    ],
)
def test_retry_output_budget(overrides, budget, expected):
    from types import SimpleNamespace

    from app.llm.model_limits import retry_output_budget

    provider = SimpleNamespace(max_output_tokens_overrides=overrides)

    assert retry_output_budget(provider, "m", budget) == expected


# --- Tier 2: metadata snapshot (precise, known models) -----------------------
def test_snapshot_known_model():
    # claude-opus-4-7 is present in the bundled snapshot at its true 128k cap.
    assert resolve_snapshot_max_output_tokens("claude-opus-4-7") == 128_000


def test_snapshot_missing_model_returns_none():
    # A dated variant newer than the snapshot - not present (date suffixes are
    # not stripped, only ':'-variants and vendor prefixes are).
    assert resolve_snapshot_max_output_tokens("claude-opus-4-8-20260901") is None


def test_snapshot_strips_version_and_variant_suffix(monkeypatch):
    """A ':'-suffix (Bedrock '...:0', OpenRouter ':beta') must not block a
    snapshot hit on the base model id - the head is kept, not the suffix."""
    monkeypatch.setattr(
        "app.llm.model_limits._load_model_metadata",
        lambda: {"anthropic.claude-foo-v1": {"max_output_tokens": 12_345}},
    )
    assert resolve_snapshot_max_output_tokens("anthropic.claude-foo-v1:0") == 12_345

    # Vendor prefix AND ":variant" suffix are both reduced to the base id.
    monkeypatch.setattr(
        "app.llm.model_limits._load_model_metadata",
        lambda: {"claude-3.5-sonnet": {"max_output_tokens": 8_192}},
    )
    assert (
        resolve_snapshot_max_output_tokens("anthropic/claude-3.5-sonnet:beta")
        == 8_192
    )
    # Multi-segment aggregator id reduces to the bare trailing model id.
    monkeypatch.setattr(
        "app.llm.model_limits._load_model_metadata",
        lambda: {"some-model": {"max_output_tokens": 4_096}},
    )
    assert (
        resolve_snapshot_max_output_tokens("openrouter/anthropic/some-model") == 4_096
    )


# --- Full chain --------------------------------------------------------------
def test_effective_admin_override_wins():
    assert (
        resolve_effective_max_output_tokens(
            model="claude-opus-4-8",
            overrides={"claude-opus-4-8": 5_000},
            provider_default=8_192,
        )
        == 5_000
    )


def test_effective_snapshot_beats_family_floor():
    # opus-4-7 is snapshot-listed (128k) and must not regress to the 64k floor.
    assert (
        resolve_effective_max_output_tokens(
            model="claude-opus-4-7", overrides=None, provider_default=8_192
        )
        == 128_000
    )


def test_effective_family_floor_rescues_new_model():
    # A dated variant missing from the snapshot -> family floor instead of the
    # 8_192 default.
    assert (
        resolve_effective_max_output_tokens(
            model="claude-opus-4-8-20260901", overrides=None, provider_default=8_192
        )
        == 64_000
    )


def test_effective_provider_default_for_legacy():
    # Legacy model unknown to snapshot + family falls back to the flat default.
    assert (
        resolve_effective_max_output_tokens(
            model="claude-2.1", overrides=None, provider_default=8_192
        )
        == 8_192
    )


def test_effective_hard_fallback():
    assert (
        resolve_effective_max_output_tokens(
            model="totally-unknown", overrides=None, provider_default=None
        )
        == _FALLBACK_MAX_OUTPUT_TOKENS
    )


# --- Wired through the providers / agent loop / failover clamp ----------------
def test_agent_loop_resolver_anthropic():
    provider = AnthropicProvider(api_key="test-key")
    # Snapshot-listed models resolve to their true cap; a dated variant newer
    # than the snapshot is rescued by the family floor.
    assert _resolve_effective_max_tokens(provider, "claude-opus-4-8") == 128_000
    assert (
        _resolve_effective_max_tokens(provider, "claude-opus-4-8-20260901") == 64_000
    )
    # Mythos 5.1 shipped after the snapshot: the family floor, not 8_192, for a
    # model whose thinking is always on and shares the budget.
    assert _resolve_effective_max_tokens(provider, "claude-mythos-5-1") == 128_000


def test_agent_loop_resolver_openai():
    provider = OpenAIProvider(api_key="test-key")
    # gpt-5.5 resolves at 128k (snapshot cap, matching the family floor) rather
    # than the OpenAI flat default of 16_384.
    assert _resolve_effective_max_tokens(provider, "gpt-5.5") == 128_000


def test_agent_loop_resolver_none_model_uses_provider_default_model():
    """``model=None`` (model-less calls default internally to
    ``llm.default_model``) must resolve against that default model — not regress
    to the provider flat default. Without the ``model or llm.default_model``
    fallback, a post-snapshot default model would silently cap at 8_192."""
    provider = AnthropicProvider(api_key="test-key")
    provider.default_model = "claude-opus-4-8-20260901"  # newer than the snapshot
    assert provider.max_output_tokens == 8_192  # stale flat default
    assert _resolve_effective_max_tokens(provider, None) == 64_000


def test_failover_clamp_uses_full_chain():
    """The wrapper mirrors the primary's flat default (8_192), but the shared
    chain must still resolve the family floor so the failover clamp never caps a
    fresh model to the stale value."""
    primary = AnthropicProvider(api_key="test-key")
    wrapper = FailoverLLMProvider(primary=primary)

    assert wrapper.max_output_tokens == 8_192  # mirrored flat default
    assert (
        _resolve_effective_max_tokens(wrapper, "claude-opus-4-8-20260901") == 64_000
    )
    assert (
        FailoverLLMProvider._resolve_max_tokens_limit(
            wrapper, "claude-opus-4-8-20260901"
        )
        == 64_000
    )
    # And a snapshot-listed model keeps its true cap (regression: previously
    # clamped to 8_192 on the failover path).
    assert (
        FailoverLLMProvider._resolve_max_tokens_limit(wrapper, "claude-opus-4-7")
        == 128_000
    )
