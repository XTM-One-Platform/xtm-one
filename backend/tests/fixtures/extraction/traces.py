"""Builders for the persisted evaluation trace shape (fixtures).

The keys are copied from what ``agent_chat`` writes into
``execution_details['traces']``: the extractor reads that blob, so a fixture that
drifts from it would test nothing.
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "extract_ttp"


def tool_trace(
    *,
    tool_name: str = TOOL_NAME,
    tool_output: str = "{}",
    tool_input: str | None = "{}",
    outcome: str | None = "success",
    duration_ms: int = 125,
) -> dict[str, Any]:
    """One tool entry in the persisted shape.

    ``outcome=None`` reproduces the span-reconstructed entry, which carries neither
    an outcome nor an input.
    """
    trace: dict[str, Any] = {
        "type": "tool",
        "description": f"Tool call: {tool_name}",
        "duration_ms": duration_ms,
        "tool_name": tool_name,
        "phase": None,
        "tool_output": tool_output,
    }
    if outcome is not None:
        trace["outcome"] = outcome
    if tool_input is not None:
        trace["tool_input"] = tool_input
    return trace


def llm_trace() -> dict[str, Any]:
    """A non-tool entry, so filters are exercised against real neighbours."""
    return {
        "type": "llm",
        "description": "LLM call - direct response",
        "duration_ms": 900,
        "outcome": "success",
        "model": "test-model",
    }


def one_successful_call(payload: str) -> list[dict[str, Any]]:
    return [llm_trace(), tool_trace(tool_output=payload)]


def three_successful_calls(first: str, second: str, third: str) -> list[dict[str, Any]]:
    return [
        tool_trace(tool_output=first),
        llm_trace(),
        tool_trace(tool_output=second),
        tool_trace(tool_output=third),
    ]


def only_first_successful(first: str, later: str) -> list[dict[str, Any]]:
    return [
        tool_trace(tool_output=first, outcome="success"),
        tool_trace(tool_output=later, outcome="error"),
        tool_trace(tool_output=later, outcome="error"),
    ]


def none_successful(payload: str) -> list[dict[str, Any]]:
    return [
        tool_trace(tool_output=payload, outcome="error"),
        tool_trace(tool_output=payload, outcome="failure"),
    ]


def no_call_at_all() -> list[dict[str, Any]]:
    return [llm_trace()]


def span_reconstructed(payload: str) -> list[dict[str, Any]]:
    """The fallback shape: no ``outcome``, no ``tool_input``."""
    return [tool_trace(tool_output=payload, outcome=None, tool_input=None)]


def other_tool_only(payload: str) -> list[dict[str, Any]]:
    return [tool_trace(tool_name="some_other_tool", tool_output=payload)]
