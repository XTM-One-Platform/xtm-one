"""Tests for _agent_tool_target_ctx — target-agent log-context helper.

The helper appends ``target_agent_id=<id>`` to timeout/error log lines for
agent-to-agent tool calls (consult_agent, spawn_background_task,
transfer_to_agent) so operators can correlate a timed-out consult with the
target sub-agent's own execution logs.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import app.tools.agent_loop as al
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# Unit tests for the helper itself
# ---------------------------------------------------------------------------


def _make_tc(name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id="tc-001", name=name, arguments=arguments or {})


@pytest.mark.parametrize("tool_name", sorted(al._AGENT_TOOL_NAMES))
def test_agent_tool_includes_target_when_present(tool_name: str) -> None:
    tc = _make_tc(tool_name, {"agent_id": "abc-123"})
    ctx = al._agent_tool_target_ctx(tc)
    assert "target_agent_id=abc-123" in ctx


@pytest.mark.parametrize("tool_name", sorted(al._AGENT_TOOL_NAMES))
def test_agent_tool_fallback_when_agent_id_missing(tool_name: str) -> None:
    tc = _make_tc(tool_name, {})
    ctx = al._agent_tool_target_ctx(tc)
    assert "target_agent_id=unknown" in ctx


def test_agent_tool_fallback_when_agent_id_empty() -> None:
    tc = _make_tc("consult_agent", {"agent_id": ""})
    assert "target_agent_id=unknown" in al._agent_tool_target_ctx(tc)


def test_agent_tool_fallback_when_agent_id_is_none() -> None:
    """A JSON ``null`` must fall back to "unknown", not log ``str(None)``."""
    tc = _make_tc("consult_agent", {"agent_id": None})
    assert "target_agent_id=unknown" in al._agent_tool_target_ctx(tc)


def test_non_agent_tool_returns_empty_string() -> None:
    for name in ("web_search", "search_knowledge", "code_interpreter", "get_email"):
        tc = _make_tc(name, {"agent_id": "should-be-ignored"})
        assert al._agent_tool_target_ctx(tc) == "", f"Expected '' for {name}"


def test_agent_id_is_sanitised_against_log_injection() -> None:
    """agent_id is LLM-provided free text — newlines and other characters
    outside the ID/slug alphabet must not reach the log line verbatim."""
    tc = _make_tc(
        "consult_agent",
        {"agent_id": "abc\nFAKE WARNING forged line\r\x1b[31m"},
    )
    ctx = al._agent_tool_target_ctx(tc)
    assert "\n" not in ctx and "\r" not in ctx and "\x1b" not in ctx
    assert ctx.startswith(", target_agent_id=abc_")


def test_non_ascii_word_characters_are_replaced() -> None:
    """The sanitiser uses ASCII semantics: Unicode "word" characters
    (homoglyphs, accented letters) must not pass the filter verbatim."""
    tc = _make_tc("consult_agent", {"agent_id": "ag\u00e9nt-\u0430\u0431\u0432"})
    ctx = al._agent_tool_target_ctx(tc)
    assert ctx == ", target_agent_id=ag_nt-___"


def test_agent_id_length_is_capped() -> None:
    tc = _make_tc("consult_agent", {"agent_id": "a" * 500})
    ctx = al._agent_tool_target_ctx(tc)
    assert ctx == ", target_agent_id=" + "a" * 80


def test_non_string_agent_id_is_stringified() -> None:
    tc = _make_tc("consult_agent", {"agent_id": 42})
    assert "target_agent_id=42" in al._agent_tool_target_ctx(tc)


# ---------------------------------------------------------------------------
# Integration smoke: timeout warning includes target_agent_id in log output
# ---------------------------------------------------------------------------


async def test_consult_agent_timeout_log_includes_target_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When consult_agent times out the warning log must contain the target
    agent's ID so the timeout can be correlated with the sub-agent's own logs.

    We drive the sequential (non-parallel) tool-execution path in run_agent_loop
    by returning exactly one tool call from the LLM and making the executor
    hang longer than the (patched) _AGENT_TOOL_TIMEOUT.
    """
    from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
    from app.tools.base import CallableToolExecutor, ToolDefinition

    monkeypatch.setattr(al, "_AGENT_TOOL_TIMEOUT", 0.05)

    target_id = "deadbeef-0000-0000-0000-000000000001"
    tool_call = ToolCall(
        id="tc-timeout",
        name="consult_agent",
        arguments={"agent_id": target_id, "question": "ping"},
    )

    call_count = 0

    class _FakeLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 200_000

        async def chat(self, messages, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ChatResponse(
                    content="",
                    tool_calls=[tool_call],
                    stop_reason="tool_use",
                    model="fake",
                    usage={},
                )
            return ChatResponse(
                content="fallback answer",
                tool_calls=[],
                stop_reason="end_turn",
                model="fake",
                usage={},
            )

        async def chat_stream(self, messages, tools=None, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    # Executor that hangs longer than the (patched) agent-tool timeout.
    # Signature must match the tool schema fields so the executor doesn't
    # filter them out before the consult_agent safety check.
    async def _hanging_tool(agent_id: str, question: str) -> str:
        await asyncio.sleep(10)
        return "never reached"

    executor = CallableToolExecutor()
    executor.register("consult_agent", _hanging_tool)
    consult_def = ToolDefinition(
        name="consult_agent",
        description="consult",
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["agent_id", "question"],
        },
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="test")]

    with caplog.at_level(logging.WARNING, logger="app.tools.agent_loop"):
        await al.run_agent_loop(
            llm=_FakeLLM(),
            messages=messages,
            tools=[consult_def],
            executor=executor,
        )

    timeout_records = [
        r
        for r in caplog.records
        if "timed out" in r.message and "consult_agent" in r.message
    ]
    assert timeout_records, "Expected at least one 'timed out' warning for consult_agent"
    assert any(target_id in r.message for r in timeout_records), (
        f"target_agent_id={target_id!r} not found in any timeout warning.\n"
        f"Captured messages: {[r.message for r in timeout_records]}"
    )
