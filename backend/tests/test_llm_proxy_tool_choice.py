"""The LLM proxy sends a forced tool call as ``auto`` to a model that refuses one.

Claude Sonnet 5.5 answers a ``tool_choice`` of type ``any`` or ``tool`` with
HTTP 400 ("tool_choice: type "tool" and "any" are not supported for this
model."). An OpenAI-format client cannot know the model behind the proxy, and
its ``required`` / named function is what a gateway turns into those types, so
the proxy relaxes it to ``auto`` - Anthropic's own migration advice - the way
it already drops a ``temperature`` the model refuses. Every other model, and
every non-forcing choice, is forwarded as the client sent it.
"""

from __future__ import annotations

import pytest

from app.api.v1.llm_proxy import _map_anthropic_stop, _proxy_tool_choice

_NAMED_FUNCTION = {"type": "function", "function": {"name": "get_weather"}}


@pytest.mark.parametrize(
    "tool_choice",
    [
        "required",
        "any",
        _NAMED_FUNCTION,
        {"type": "tool", "name": "get_weather"},
        {"type": "any"},
    ],
)
@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-5-5", "anthropic/claude-sonnet-5.5", "us.anthropic.claude-sonnet-5-5-v1:0"],
)
def test_a_forced_tool_call_becomes_auto_on_sonnet_5_5(model: str, tool_choice):
    assert _proxy_tool_choice(model, tool_choice) == "auto"


@pytest.mark.parametrize("tool_choice", ["auto", "none", {"type": "auto"}, {"type": "none"}])
def test_a_choice_that_forces_nothing_is_forwarded(tool_choice):
    assert _proxy_tool_choice("claude-sonnet-5-5", tool_choice) == tool_choice


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5-5", "gpt-6", "anthropic/claude-opus-4.8"])
@pytest.mark.parametrize("tool_choice", ["required", _NAMED_FUNCTION])
def test_a_model_that_takes_a_forced_tool_call_gets_it(model: str, tool_choice):
    assert _proxy_tool_choice(model, tool_choice) == tool_choice


def _allowed_tools(mode: str) -> dict:
    return {
        "type": "allowed_tools",
        "allowed_tools": {"mode": mode, "tools": [{"type": "function", "name": "get_weather"}]},
    }


def test_allowed_tools_that_force_a_call_keep_the_subset_without_forcing_it():
    assert _proxy_tool_choice("anthropic/claude-sonnet-5.5", _allowed_tools("required")) == (
        _allowed_tools("auto")
    )


@pytest.mark.parametrize(
    ("model", "tool_choice"),
    [
        ("anthropic/claude-sonnet-5.5", _allowed_tools("auto")),
        ("claude-sonnet-5", _allowed_tools("required")),
    ],
)
def test_allowed_tools_that_force_nothing_or_go_to_another_model_are_forwarded(model, tool_choice):
    assert _proxy_tool_choice(model, tool_choice) == tool_choice


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        (None, "stop"),
    ],
)
def test_anthropic_stop_reasons_map_to_openai_finish_reasons(stop_reason, finish_reason):
    assert _map_anthropic_stop(stop_reason) == finish_reason
