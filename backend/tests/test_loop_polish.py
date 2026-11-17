"""Polish fixes for silent failure modes in the agent loop and the
assignment engine (assessment findings CORE-9 / CORE-10 / ORCH-8).

* CORE-9 — when the forced-final-answer backstop LLM call times out (or
  errors), the user must receive an explicit message instead of a bare
  tool-results digest indistinguishable from a normal answer.
* CORE-10 — when the tool count exceeds the provider's ``max_tools`` limit,
  a system notice must tell the model which tools were truncated away so it
  doesn't hallucinate calls to dropped names.
* ORCH-8 — the assignment-engine TimeoutError message must reflect the
  *effective* timeout (including the disabled case, where the error can only
  come from an inner timeout) instead of always citing
  ``assignment.timeout_seconds`` which may be 0/None.
"""

from __future__ import annotations

import inspect

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import (
    _FORCED_FINAL_ANSWER,
    _TRUNCATION_NOTICE_MAX_NAMES,
    FORCED_ANSWER_TIMEOUT_MESSAGE,
    _build_tool_truncation_notice,
    run_agent_loop,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# CORE-9 — forced-final-answer timeout produces an explicit user message
# ---------------------------------------------------------------------------


class _ForcedAnswerTimeoutLLM(BaseLLMProvider):
    """Stub LLM that keeps calling a tool until the iteration budget is
    exhausted, returns empty text on tool-stripped iterations, and raises a
    scripted ``TimeoutError`` on the forced-final-answer backstop call
    (detected via the ``_FORCED_FINAL_ANSWER`` system prompt marker)."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.call_count = 0
        self.forced_calls = 0

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        self.call_count += 1
        if any((m.content or "") == _FORCED_FINAL_ANSWER for m in messages):
            # Backstop synthesis (and its stronger-prompt retry, which keeps
            # the marker in the conversation) — scripted timeout.
            self.forced_calls += 1
            raise TimeoutError("scripted forced-answer timeout")
        if tools:
            # Keep burning the budget with tool calls (varied arguments so
            # no repeated-call heuristics interfere).
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[
                    ToolCall(
                        id=f"tc-{self.call_count}",
                        name="echo_tool",
                        arguments={"i": self.call_count},
                    )
                ],
                stop_reason="tool_use",
            )
        # Tool-stripped iteration (and its empty-response retry): empty text
        # so the loop falls through to the forced-final-answer backstop.
        return ChatResponse(content="", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _build_echo_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _echo(i: int = 0) -> str:
        return f"echo-result-payload-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [
        ToolDefinition(name="echo_tool", description="Echo a value back")
    ]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


@pytest.mark.asyncio
async def test_forced_answer_timeout_produces_explicit_message():
    """When the forced-final-answer backstop call times out, the response
    MUST lead with the explicit timeout message and keep the synthesised
    tool-results digest as partial results — never an unexplained digest or
    empty content (CORE-9)."""
    definitions, composite = _build_echo_executor()
    llm = _ForcedAnswerTimeoutLLM()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
    )

    assert llm.forced_calls >= 1, "the scripted backstop timeout never fired"
    assert response.content.startswith(FORCED_ANSWER_TIMEOUT_MESSAGE)
    # The tool results gathered before the timeout must survive as a
    # clearly-labelled partial digest.
    assert "Partial results:" in response.content
    assert "echo-result-payload" in response.content


class _ForcedAnswerTimeoutThenEmptyLLM(_ForcedAnswerTimeoutLLM):
    """Like :class:`_ForcedAnswerTimeoutLLM`, but only the FIRST backstop
    call times out — the stronger-prompt retry completes successfully with
    empty content."""

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        if (
            any((m.content or "") == _FORCED_FINAL_ANSWER for m in messages)
            and self.forced_calls >= 1
        ):
            self.call_count += 1
            self.forced_calls += 1
            return ChatResponse(content="", model=model or "stub")
        return await super().chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_forced_answer_retry_success_clears_failure_lead_in():
    """When the first backstop call times out but the stronger-prompt retry
    completes successfully (just with empty content), the response must be
    the plain synthesised digest — NOT the timeout lead-in, which would
    falsely blame a timeout for an ordinary empty model output.
    """
    definitions, composite = _build_echo_executor()
    llm = _ForcedAnswerTimeoutThenEmptyLLM()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
    )

    assert llm.forced_calls >= 2, "the retry backstop call never ran"
    assert not response.content.startswith(FORCED_ANSWER_TIMEOUT_MESSAGE)
    assert "Partial results:" not in response.content
    # The plain digest path must still surface the tool results.
    assert "echo-result-payload" in response.content


# ---------------------------------------------------------------------------
# CORE-10 — tool truncation notice
# ---------------------------------------------------------------------------


class _CappedToolsLLM(BaseLLMProvider):
    """Stub LLM with a tiny ``max_tools`` limit that answers immediately."""

    provider_name = "stub"
    context_window = 100_000
    max_tools = 2

    async def chat(self, messages, model=None, tools=None, **kwargs):
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_tool_truncation_appends_notice_once_with_dropped_names():
    """When the tool count exceeds the provider's ``max_tools``, exactly one
    system message must be appended naming the dropped tools (and only the
    dropped ones) so the model doesn't hallucinate them (CORE-10)."""
    tools = [
        ToolDefinition(name="keep_a", description="kept", priority=0),
        ToolDefinition(name="keep_b", description="kept", priority=0),
        ToolDefinition(name="drop_c", description="dropped", priority=2),
        ToolDefinition(name="drop_d", description="dropped", priority=2),
    ]
    messages = [ChatMessage(role="user", content="hello")]

    response = await run_agent_loop(
        llm=_CappedToolsLLM(),
        messages=messages,
        tools=tools,
        executor=None,
        max_iterations=3,
        progressive_tools=False,
    )

    assert response.content == "final answer"
    notices = [
        m
        for m in messages
        if m.role == "system"
        and "unavailable this run due to provider tool-count limits" in (m.content or "")
    ]
    assert len(notices) == 1, "the truncation notice must be appended exactly once"
    notice = notices[0].content
    assert "drop_c" in notice
    assert "drop_d" in notice
    assert "keep_a" not in notice
    assert "keep_b" not in notice
    assert "Do not attempt to call them" in notice


@pytest.mark.asyncio
async def test_no_truncation_notice_when_under_provider_limit():
    """No notice must be injected when every tool fits within ``max_tools``."""
    tools = [
        ToolDefinition(name="keep_a", description="kept"),
        ToolDefinition(name="keep_b", description="kept"),
    ]
    messages = [ChatMessage(role="user", content="hello")]

    await run_agent_loop(
        llm=_CappedToolsLLM(),
        messages=messages,
        tools=tools,
        executor=None,
        max_iterations=3,
        progressive_tools=False,
    )

    assert not any(
        "unavailable this run due to provider tool-count limits" in (m.content or "")
        for m in messages
    )


def test_truncation_notice_caps_listed_names():
    """The notice must list at most ``_TRUNCATION_NOTICE_MAX_NAMES`` names and
    summarise the remainder as a count — never an unbounded join."""
    names = [f"tool_{i:02d}" for i in range(_TRUNCATION_NOTICE_MAX_NAMES + 5)]
    notice = _build_tool_truncation_notice(names)
    assert f"{len(names)} tool(s) were unavailable" in notice
    assert names[_TRUNCATION_NOTICE_MAX_NAMES - 1] in notice
    assert names[_TRUNCATION_NOTICE_MAX_NAMES] not in notice
    assert "and 5 more" in notice


def test_truncation_notice_short_list_has_no_summary_suffix():
    notice = _build_tool_truncation_notice(["alpha", "beta"])
    assert "alpha, beta" in notice
    assert "more" not in notice


# ---------------------------------------------------------------------------
# ORCH-8 — assignment timeout message reflects the effective timeout
# ---------------------------------------------------------------------------


def test_assignment_timeout_message_enabled_mode_uses_effective_timeout():
    """Enabled mode must cite the effective timeout passed to
    ``asyncio.wait_for`` (covers the ``_UNSET_TIMEOUT_FALLBACK_SECONDS``
    fallback for unset rows, where ``assignment.timeout_seconds`` is None)."""
    from app.services.assignment_engine import _format_assignment_timeout_error

    msg = _format_assignment_timeout_error(900, 901.4, 25)
    assert "Assignment timed out after 900s" in msg
    assert "max_iterations=25" in msg
    assert "None" not in msg


def test_assignment_timeout_message_disabled_mode_blames_inner_timeout():
    """When the assignment-level timeout is disabled (timeout=None), a
    TimeoutError can only come from an inner operation — the message must say
    so and report the actual elapsed time instead of citing 0/None
    (ORCH-8)."""
    from app.services.assignment_engine import _format_assignment_timeout_error

    msg = _format_assignment_timeout_error(None, 1234.9, 10)
    assert "inner timeout" in msg
    assert "~1234s" in msg
    assert "assignment-level timeout is disabled" in msg
    assert "max_iterations=10" in msg
    assert "None" not in msg


def test_assignment_engine_timeout_handler_uses_formatter():
    """Static guard: the engine's ``except TimeoutError`` handler must build
    its message via ``_format_assignment_timeout_error`` with the effective
    ``timeout`` value — a regression back to the inline f-string citing
    ``assignment.timeout_seconds`` reintroduces the misleading 0/None
    diagnostics."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "_loop_timeout_error(" in source
    assert "_format_assignment_timeout_error(" in inspect.getsource(
        assignment_engine._loop_timeout_error
    )
    assert "after {assignment.timeout_seconds}s" not in source


def test_assignment_engine_passes_budgeted_tool_timeout_to_loop():
    """High-iteration assignment runs must pass the derived per-tool timeout
    cap to ``run_agent_loop`` so one long tool call cannot consume the whole
    assignment wall-clock budget. The cap must be derived from the EFFECTIVE
    run budget (normalized + iteration-floored via
    ``_apply_iteration_timeout_floor``), never ``assignment.timeout_seconds``
    directly: the column is non-nullable with a 300s default, so a ``None``
    attribute only exists on an unset in-memory row before flush and means
    "fallback applies" rather than "disabled"."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    # The cap is derived AFTER the floor so both share one effective number.
    floored_segment = source.split("_apply_iteration_timeout_floor(", 1)[1]
    assert "resolve_assignment_tool_timeout(" in floored_segment
    # The raw column value must never flow into the tool-timeout helper.
    assert "resolve_assignment_tool_timeout(assignment.timeout_seconds" not in source
    assert 'loop_kwargs["tool_timeout_seconds"] = assignment_tool_timeout' in source
