"""The pre-flight overflow check budgets each model against its real window.

Regression: ``BaseLLMProvider._resolve_context_window`` read the admin override
and then the provider's flat class default, while the agentic loop budgets with
``model_limits.resolve_effective_context_window`` (override, catalog, family
table, class default). The Filigran AI Gateway's 128_000 default thus rejected
a tool-heavy Claude Opus 5 request (~122k estimated tokens) with
``ContextOverflowError`` before it was ever sent, although the model takes 1M
tokens. A known window now raises the class default, never lowers it.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ChatMessage, ContextOverflowError
from app.llm.gateway_provider import FiligranGatewayProvider
from app.llm.model_limits import known_context_window, resolve_effective_context_window
from app.llm.openrouter_provider import OpenRouterProvider
from app.tools.base import ToolDefinition

GATEWAY_URL = "http://gateway.local:8080"


def _provider() -> FiligranGatewayProvider:
    return FiligranGatewayProvider(api_key=GATEWAY_URL)


def _tools_of(tokens: int, provider) -> list[ToolDefinition]:
    return [ToolDefinition(name="t", description="x" * (tokens * provider._CHARS_PER_TOKEN))]


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_known_model_uses_the_platform_window_not_the_flat_default(model):
    provider = _provider()
    expected = known_context_window(model)
    assert expected is not None
    assert provider._resolve_context_window(model) == expected[0]
    assert provider._resolve_context_window(model) > provider.context_window


_PROVIDERS = [
    pytest.param(_provider, id="gateway"),
    pytest.param(lambda: AnthropicProvider(api_key="sk-test"), id="anthropic"),
    pytest.param(lambda: OpenRouterProvider(api_key="sk-test"), id="openrouter"),
]
_MODELS = [
    "claude-opus-5-5",
    "claude-sonnet-4-5",
    "anthropic/claude-opus-4.6",
    "claude-opus-4-6@default",
    "gpt-4o",
    "codestral-latest",
    "default",
    None,
]


@pytest.mark.parametrize("make", _PROVIDERS)
@pytest.mark.parametrize("model", _MODELS)
def test_the_pre_flight_never_refuses_what_main_or_the_loop_let_through(make, model):
    """Never below the flat default the check read before, nor below the
    window the agentic loop budgets the run with."""
    provider = make()
    window = provider._resolve_context_window(model)
    assert window >= provider.context_window
    assert window >= resolve_effective_context_window(
        model=model, overrides=provider.context_windows, provider_default=provider.context_window
    )


def test_admin_override_still_wins():
    provider = _provider()
    provider.context_windows = {"claude-opus-5-5": 200_000}
    assert provider._resolve_context_window("claude-opus-5-5") == 200_000


def test_unknown_model_falls_back_to_the_class_default():
    provider = _provider()
    assert provider._resolve_context_window("default") == provider.context_window
    assert provider._resolve_context_window(None) == provider.context_window


def test_tool_heavy_opus_request_under_its_real_window_is_not_rejected():
    """~122k estimated tokens: rejected against 128k, accepted against 1M."""
    provider = _provider()
    provider._check_resolved_overflow(
        text_chars=0,
        image_count=0,
        native_file_bytes=0,
        tools=_tools_of(122_840, provider),
        model="claude-opus-5-5",
    )


def test_an_unknown_model_is_still_guarded():
    provider = _provider()
    with pytest.raises(ContextOverflowError):
        provider._check_resolved_overflow(
            text_chars=0, image_count=0, native_file_bytes=0, tools=_tools_of(200_000, provider), model="default"
        )


def test_a_known_window_never_lowers_a_larger_provider_default():
    """The catalog's 200_000 for a Claude 4.x id is no reason to refuse what a
    1M-default provider (OpenRouter, Vertex) serves at 1M."""
    provider = OpenRouterProvider(api_key="sk-test")
    assert provider._resolve_context_window("anthropic/claude-opus-4.6") == provider.context_window == 1_000_000


def test_the_gateway_sends_the_incident_request_instead_of_refusing_it():
    """The production request end to end: ``chat()`` with ~122.8k tokens of
    tool definitions reaches the gateway's endpoint."""
    provider = _provider()
    sent: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                model="claude-opus-5-5",
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    client.with_options = lambda **_options: client  # the chat client: no SDK retry
    provider.client = client
    messages = [ChatMessage(role="user", content="hello")]

    response = asyncio.run(provider.chat(messages, model="claude-opus-5-5", tools=_tools_of(122_840, provider)))

    assert response.content == "ok"
    assert len(sent) == 1 and sent[0]["model"] == "claude-opus-5-5"
