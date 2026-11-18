from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.model_capabilities import (
    EFFORT_LEVELS,
    MODEL_PARAMETER_RULES,
    OUTPUT_TOKEN_PARAMETERS,
    between_tools_thinking_off,
    build_openai_generation_params,
    capped_effort,
    defaults_to_adaptive_thinking,
    omit_temperature,
    refuses_effort_with_tools,
    refuses_forced_tool_choice,
    requires_summarized_thinking_opt_in,
    supported_effort,
    supports_bedrock_structured_output,
    thinking_always_on,
    thinking_off_max_effort,
)
from scripts.check_model_capabilities import CapabilityCheckError, _validate_parameters


def test_gpt_5_uses_completion_tokens_and_omits_temperature():
    assert build_openai_generation_params(
        "gpt-5-mini", temperature=0.2, max_tokens=512
    ) == {"max_completion_tokens": 512}


def test_standard_openai_model_keeps_legacy_parameters():
    assert build_openai_generation_params(
        "gpt-4o", temperature=0.2, max_tokens=512
    ) == {"temperature": 0.2, "max_tokens": 512}


def test_unset_model_keeps_default_generation_parameters():
    assert build_openai_generation_params(
        None, temperature=0.2, max_tokens=512
    ) == {"temperature": 0.2, "max_tokens": 512}


def test_gateway_prefixed_claude_model_omits_temperature():
    assert build_openai_generation_params(
        "anthropic/claude-sonnet-5", temperature=0.2, max_tokens=512
    ) == {"max_tokens": 512}


def test_gateway_prefixed_gpt_5_keeps_gateway_output_token_name():
    assert build_openai_generation_params(
        "openai/gpt-5-mini", temperature=0.2, max_tokens=512
    ) == {"max_tokens": 512}


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-5-5",
        "claude-haiku-5",
        "anthropic/claude-haiku-5-5",
        "anthropic/claude-haiku-5.5",
        "anthropic.claude-haiku-5-5",
    ],
)
def test_haiku_5_is_not_sent_a_temperature(model: str):
    """claude-haiku-5-5 refuses a custom temperature with HTTP 400 (the gateway
    answers 502 provider_error), like the other 5.x models."""
    assert omit_temperature(model) is True
    assert build_openai_generation_params(model, temperature=0.7, max_tokens=512) == {
        "max_tokens": 512
    }


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "anthropic/claude-haiku-4.5"]
)
def test_haiku_4_still_takes_a_temperature(model: str):
    assert omit_temperature(model) is False
    params = build_openai_generation_params(model, temperature=0.7, max_tokens=512)
    assert params["temperature"] == 0.7


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "anthropic/claude-haiku-4.5", "us.anthropic.claude-haiku-4-5"]
)
def test_claude_temperature_above_one_is_clamped(model: str):
    """Agents allow 0..2; Claude refuses ``temperature: range: 0..1`` (#11657)."""
    params = build_openai_generation_params(model, temperature=1.5, max_tokens=512)
    assert params["temperature"] == 1.0


def test_non_claude_temperature_above_one_is_kept():
    params = build_openai_generation_params("gpt-4o", temperature=1.5, max_tokens=512)
    assert params["temperature"] == 1.5


def test_bedrock_vendor_prefixed_claude_omits_temperature():
    assert omit_temperature("anthropic.claude-sonnet-5") is True


def test_bedrock_regional_prefixed_claude_omits_temperature():
    assert omit_temperature("us.anthropic.claude-opus-4-8") is True


@pytest.mark.parametrize(
    ("model", "no_temperature"),
    [
        ("anthropic/claude-opus-4.7", True),
        ("anthropic/claude-opus-4.8", True),
        ("anthropic/claude-sonnet-5.5", True),
        ("anthropic/claude-sonnet-4.6", False),
        ("anthropic/claude-3.7-sonnet", False),
        ("openai/gpt-5.4", True),
        ("gpt-4.1", False),
    ],
)
def test_a_dotted_gateway_version_takes_the_rule_of_its_model(
    model: str, no_temperature: bool
):
    """OpenRouter spells Claude versions with a dot: ``claude-opus-4.7`` must
    not miss the Opus 4.7 rule and be sent a temperature it refuses."""
    assert omit_temperature(model) is no_temperature
    params = build_openai_generation_params(model, temperature=0.2, max_tokens=512)
    assert ("temperature" not in params) is no_temperature


@pytest.mark.asyncio
async def test_bedrock_chat_default_model_omits_temperature():
    """BedrockProvider's own default_model (claude-sonnet-5) must not send
    temperature — the exact combination that 400'd before the provider was
    routed through the shared omit_temperature() rule.
    """
    from app.llm.bedrock_provider import BedrockProvider

    provider = BedrockProvider(api_key="AKIA||secret||us-east-1")
    mock_client = MagicMock()
    mock_client.converse = MagicMock(
        return_value={
            "output": {"message": {"content": [{"text": "hi"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    provider._get_client = AsyncMock(return_value=mock_client)

    await provider.chat(messages=[], model=provider.default_model)

    sent_kwargs = mock_client.converse.call_args.kwargs
    assert "temperature" not in sent_kwargs["inferenceConfig"]


def test_gpt_6_omits_temperature_while_using_completion_tokens():
    assert build_openai_generation_params(
        "gpt-6-mini", temperature=0.2, max_tokens=512
    ) == {"max_completion_tokens": 512}


@pytest.mark.parametrize(
    "model", ["gpt-6-luna", "gpt-6-luna-2026-09-01", "gpt-6.1-sol", "gpt-6.2-sol", "gpt-6.2-astra", "openai/gpt-6-luna"]
)
def test_gpt_6_is_not_sent_a_temperature(model: str):
    """gpt-6 models refuse any temperature but 1 (#125)."""
    assert omit_temperature(model) is True
    assert "temperature" not in build_openai_generation_params(model, temperature=0.2, max_tokens=512)
    assert supported_effort(model, "max") == "max"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-luna-2026-09-01", "openai/gpt-6-luna"])
def test_gpt_6_luna_is_sent_none_with_tools(model: str):
    assert refuses_effort_with_tools(model) is True


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra"])
def test_gpt_6_1_and_astra_omit_the_temperature_they_refuse(model):
    assert build_openai_generation_params(model, temperature=0.0, max_tokens=512) == {
        "max_completion_tokens": 512
    }


def test_chatgpt_model_keeps_temperature_with_completion_tokens():
    assert build_openai_generation_params(
        "chatgpt-4o-latest", temperature=0.2, max_tokens=512
    ) == {"temperature": 0.2, "max_completion_tokens": 512}


@pytest.mark.parametrize(
    "model",
    [
        None,
        "unknown-model",
        "openai/gpt-5-mini",
        *(prefix for rule in MODEL_PARAMETER_RULES for prefix in rule.prefixes),
    ],
)
def test_every_rule_sends_exactly_one_output_token_parameter(model: str | None):
    """The capability checker reads the output-token parameter it compares with
    the provider catalog out of these parameters."""
    params = build_openai_generation_params(model, temperature=0.2, max_tokens=7)
    output_keys = [key for key in params if key in OUTPUT_TOKEN_PARAMETERS]
    assert len(output_keys) == 1
    assert params[output_keys[0]] == 7


def test_capability_check_accepts_matching_gateway_metadata():
    assert _validate_parameters(
        "openrouter", "openai/gpt-5-mini", ["max_tokens"]
    ) == []


def test_capability_check_reports_unsupported_output_token_parameter():
    assert _validate_parameters(
        "openrouter", "gpt-5-mini", ["max_tokens"]
    ) == ["static rules require unsupported parameter: max_completion_tokens"]


def test_capability_check_rejects_missing_output_token_parameter():
    with (
        patch(
            "app.llm.model_capabilities.build_openai_generation_params",
            return_value={"temperature": 0.2},
        ),
        pytest.raises(
            CapabilityCheckError,
            match=r"test-model: no output-token parameter .* got keys \['temperature'\]",
        ),
    ):
        _validate_parameters("openrouter", "test-model", ["temperature"])


def test_capability_check_reports_temperature_drift():
    assert _validate_parameters(
        "anthropic", "claude-sonnet-5", ["max_tokens", "temperature"]
    ) == ["static rules omit temperature but provider advertises it"]


def test_gateway_prefixed_claude_enables_adaptive_thinking_defaults():
    assert defaults_to_adaptive_thinking("anthropic/claude-sonnet-5") is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-mythos-5",
        "claude-mythos-preview",
        "us.anthropic.claude-sonnet-5",
    ],
)
def test_models_that_hide_their_thinking_by_default_opt_in(model: str):
    """They think by default with ``thinking.display`` defaulting to
    "omitted", so a reasoning pane stays blank unless the request asks."""
    assert requires_summarized_thinking_opt_in(model) is True


@pytest.mark.parametrize(
    "model",
    ["claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "gpt-5-mini", None],
)
def test_models_outside_the_opt_in_rule(model: str | None):
    assert requires_summarized_thinking_opt_in(model) is False


def test_splitting_the_rule_keeps_opus_4_7_and_4_8_parameters():
    """Opus 4.7 / 4.8 moved to their own rule (no display opt-in); their
    temperature and ``disable_thinking`` handling must not move with it."""
    for model in ("claude-opus-4-7", "claude-opus-4-8"):
        assert omit_temperature(model) is True
        assert defaults_to_adaptive_thinking(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-mythos-5",
        "claude-mythos-5-1",
        "claude-mythos-preview",
        "anthropic.claude-mythos-5",
        "us.anthropic.claude-mythos-5-1",
        "anthropic/claude-mythos-preview",
    ],
)
def test_mythos_takes_the_thinking_off_rule_in_the_form_it_accepts(model: str):
    """Mythos thinks by default and refuses ``thinking.type.disabled``:
    ``disable_thinking`` reaches it and goes straight to adaptive thinking at
    the lowest effort, while its temperature omission and display opt-in stay
    as they were. Gateway- and Bedrock-prefixed ids share the rule."""
    assert defaults_to_adaptive_thinking(model) is True
    assert thinking_always_on(model) is True
    assert omit_temperature(model) is True
    assert requires_summarized_thinking_opt_in(model) is True


@pytest.mark.parametrize(
    ("model", "adaptive", "always_on", "no_temperature", "display_opt_in"),
    [
        ("claude-sonnet-5-5", True, False, True, True),
        ("claude-sonnet-5", True, False, True, True),
        ("claude-opus-5", True, False, True, True),
        ("claude-opus-5-5", True, True, True, True),
        ("claude-fable-5", True, True, True, True),
        ("claude-fable-5-1", True, True, True, True),
        ("anthropic/claude-opus-5.5", True, True, True, True),
        ("claude-opus-4-7", True, False, True, False),
        ("claude-opus-4-8", True, False, True, False),
        ("claude-opus-4-6", False, False, False, False),
        ("claude-sonnet-4-6", False, False, False, False),
        ("claude-opus-4-5", False, False, False, False),
        ("claude-haiku-4-5", False, False, False, False),
        ("gpt-5-mini", False, False, True, False),
        (None, False, False, False, False),
    ],
)
def test_thinking_rules_follow_anthropics_model_matrix(
    model: str | None,
    adaptive: bool,
    always_on: bool,
    no_temperature: bool,
    display_opt_in: bool,
):
    """Opus 5.5 and Fable 5 / 5.1 refuse ``thinking.type.disabled`` at every
    effort, like Mythos, so they are declared rather than learned from a 400;
    Opus 5 and Sonnet 5 accept it, and Sonnet 5.5 has a form of its own."""
    assert defaults_to_adaptive_thinking(model) is adaptive
    assert thinking_always_on(model) is always_on
    assert omit_temperature(model) is no_temperature
    assert requires_summarized_thinking_opt_in(model) is display_opt_in


@pytest.mark.parametrize(
    ("model", "between_tools", "ceiling"),
    [
        ("claude-sonnet-5-5", True, "high"),
        ("anthropic/claude-sonnet-5.5", True, "high"),
        ("claude-sonnet-5", False, None),
        ("claude-opus-5", False, "high"),
        ("claude-opus-5-5", False, None),
        ("claude-fable-5-1", False, None),
        ("claude-opus-4-8", False, None),
        ("gpt-5-mini", False, None),
        (None, False, None),
    ],
)
def test_thinking_off_form_and_its_effort_ceiling(
    model: str | None, between_tools: bool, ceiling: str | None
):
    """Sonnet 5.5 refuses ``disabled`` and turns up-front thinking off with
    ``between_tools``, refused above effort ``high``; Opus 5 accepts
    ``disabled`` at ``high`` or below only."""
    assert between_tools_thinking_off(model) is between_tools
    assert thinking_off_max_effort(model) == ceiling


@pytest.mark.parametrize(
    ("model", "sonnet_5_5"),
    [
        ("claude-sonnet-5-5", True),
        ("anthropic/claude-sonnet-5.5", True),
        ("us.anthropic.claude-sonnet-5-5-v1:0", True),
        ("claude-sonnet-5", False),
        ("anthropic.claude-sonnet-5", False),
        ("claude-opus-5-5", False),
        ("claude-opus-4-8", False),
        ("gpt-5-mini", False),
        (None, False),
    ],
)
def test_forced_tool_choice_and_bedrock_structured_outputs(model: str | None, sonnet_5_5: bool):
    """Sonnet 5.5 refuses a ``tool_choice`` of type ``any`` / ``tool``, and
    Bedrock serves it without structured outputs; every earlier model takes both."""
    assert refuses_forced_tool_choice(model) is sonnet_5_5
    assert supports_bedrock_structured_output(model) is not sonnet_5_5


@pytest.mark.parametrize(
    ("model", "requested", "sent"),
    [
        ("claude-opus-5-5", "max", "max"),
        ("claude-sonnet-5-5", "xhigh", "xhigh"),
        ("claude-fable-5-1", "xhigh", "xhigh"),
        ("claude-mythos-5-1", "xhigh", "xhigh"),
        ("claude-opus-4-8", "xhigh", "xhigh"),
        ("claude-opus-4-6", "xhigh", "high"),
        ("claude-opus-4-6", "max", "max"),
        ("claude-sonnet-4-6", "xhigh", "high"),
        ("claude-mythos-preview", "xhigh", "high"),
        ("claude-opus-4-5", "max", "high"),
        ("claude-opus-4-5", "medium", "medium"),
        ("us.anthropic.claude-opus-4-5-20251101-v1:0", "max", "high"),
        ("claude-sonnet-4-5", "high", None),
        ("claude-haiku-4-5", "low", None),
        ("claude-sonnet-4-20250514", "max", None),
        ("claude-opus-4-1", "high", None),
        ("claude-3-7-sonnet-20250219", "high", None),
        ("claude-opus-5", "none", None),
        ("claude-opus-5", None, None),
        ("unknown-model", "max", "max"),
        (None, "max", "max"),
    ],
)
def test_effort_steps_down_to_a_level_the_model_accepts(
    model: str | None, requested: str | None, sent: str | None
):
    """An effort level a model does not take is an HTTP 400, which the router
    answered by failing the call over to another model."""
    assert supported_effort(model, requested) == sent


@pytest.mark.parametrize(
    ("effort", "ceiling", "capped"),
    [
        ("max", "high", "high"),
        ("xhigh", "high", "high"),
        ("high", "high", "high"),
        ("low", "high", "low"),
        ("max", None, "max"),
    ],
)
def test_capped_effort(effort: str, ceiling: str | None, capped: str):
    assert capped_effort(effort, ceiling) == capped


def test_every_declared_effort_level_is_an_anthropic_level():
    for rule in MODEL_PARAMETER_RULES:
        if rule.effort_levels is not None:
            assert set(rule.effort_levels) <= set(EFFORT_LEVELS), rule.prefixes
        if rule.thinking_off_max_effort is not None:
            assert rule.effort_levels, rule.prefixes
            assert rule.thinking_off_max_effort in rule.effort_levels, rule.prefixes


def test_every_between_tools_model_takes_the_thinking_off_rule():
    """``between_tools_thinking_off`` is read inside ``chat()``'s thinking-off
    rule like ``thinking_always_on``, and the form is refused above an effort,
    which the rule must name."""
    for rule in MODEL_PARAMETER_RULES:
        if rule.between_tools_thinking_off:
            assert rule.adaptive_thinking, rule.prefixes
            assert rule.omit_temperature, rule.prefixes
            assert not rule.thinking_always_on, rule.prefixes
            assert rule.thinking_off_max_effort is not None, rule.prefixes


def test_every_always_on_thinking_model_takes_the_thinking_off_rule():
    """``thinking_always_on`` is read inside ``chat()``'s thinking-off rule,
    which only runs for ``adaptive_thinking`` models: declared without the
    flag it would change nothing. Such a model also refuses a non-default
    ``temperature`` on every request."""
    for rule in MODEL_PARAMETER_RULES:
        if rule.thinking_always_on:
            assert rule.adaptive_thinking, rule.prefixes
            assert rule.omit_temperature, rule.prefixes


def test_every_display_opt_in_model_omits_temperature():
    """The display opt-in sends an explicit ``thinking`` config, which the API
    refuses alongside ``temperature``: a model in that rule that was still sent
    one would fail every streamed chat turn with HTTP 400. Mythos, like the rest
    of the family, takes no sampling parameters at all."""
    for rule in MODEL_PARAMETER_RULES:
        if rule.summarized_thinking_opt_in:
            assert rule.omit_temperature, rule.prefixes
    assert omit_temperature("claude-mythos-5") is True
    assert omit_temperature("claude-mythos-preview") is True


def test_checker_rejects_missing_anthropic_capabilities(monkeypatch):
    from scripts.check_model_capabilities import CapabilityCheckError, _load_parameters

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch(
        "scripts.check_model_capabilities._load_catalog_model",
        return_value={"id": "claude-sonnet-5"},
    ), pytest.raises(CapabilityCheckError, match="did not publish capabilities"):
        _load_parameters("anthropic", "claude-sonnet-5")


def test_checker_follows_anthropic_catalog_pagination(monkeypatch):
    from scripts.check_model_capabilities import _load_catalog_model

    pages = iter(
        [
            {"data": [{"id": "claude-sonnet-4"}], "has_more": True, "last_id": "first"},
            {"data": [{"id": "claude-sonnet-4"}], "has_more": True, "last_id": "second"},
            {"data": [{"id": "claude-sonnet-5", "capabilities": {"temperature": True}}]},
        ]
    )
    urls = []

    def load_json(url, **kwargs):
        urls.append(url)
        return next(pages)

    monkeypatch.setattr("scripts.check_model_capabilities._get_json", load_json)

    metadata = _load_catalog_model(
        "https://api.anthropic.com/v1/models", "claude-sonnet-5"
    )

    assert metadata["id"] == "claude-sonnet-5"
    assert urls == [
        "https://api.anthropic.com/v1/models",
        "https://api.anthropic.com/v1/models?after_id=first",
        "https://api.anthropic.com/v1/models?after_id=second",
    ]


def test_checker_rejects_non_advancing_anthropic_cursor(monkeypatch):
    from scripts.check_model_capabilities import CapabilityCheckError, _load_catalog_model

    pages = iter(
        [
            {"data": [{"id": "claude-sonnet-4"}], "has_more": True, "last_id": "first"},
            {"data": [{"id": "claude-sonnet-4"}], "has_more": True, "last_id": "first"},
        ]
    )

    def load_json(url, **kwargs):
        return next(pages)

    monkeypatch.setattr("scripts.check_model_capabilities._get_json", load_json)

    with pytest.raises(CapabilityCheckError, match="did not advance"):
        _load_catalog_model("https://api.anthropic.com/v1/models", "claude-sonnet-5")


def test_checker_treats_gemini_temperature_default_as_supported(monkeypatch):
    from scripts.check_model_capabilities import _load_parameters

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "scripts.check_model_capabilities._get_json",
        lambda *args, **kwargs: {"temperature": 1.0, "outputTokenLimit": 1024},
    )

    assert _load_parameters("gemini", "gemini-3.8-flash") == [
        "temperature",
        "max_output_tokens",
    ]
