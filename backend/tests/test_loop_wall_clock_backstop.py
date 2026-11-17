"""Tests for wall-clock deadline awareness in the forced-final-answer backstop.

The assignment engine wraps ``run_agent_loop`` with
``asyncio.wait_for(run_agent_loop(...), timeout=T)``.  Before this fix the
forced-final-answer backstop used ``_LLM_CALL_TIMEOUT = 900 s`` for its own
LLM calls — which can exceed the outer assignment timeout, causing the outer
``wait_for`` to cancel the synthesis mid-flight and leave the run record with
a hard ``TimeoutError`` and *no output at all*.

The fix passes ``wall_clock_deadline`` to ``run_agent_loop``.  The backstop
checks the remaining budget:
* If remaining < ``_FORCED_ANSWER_MIN_REMAINING_SECS``: return an immediate
  pre-built fallback (no LLM call at all).
* Otherwise: LLM call timeout = ``min(remaining - _FORCED_ANSWER_DEADLINE_RESERVE_SECS,
  _LLM_CALL_TIMEOUT)`` so the call always finishes before the outer deadline.

Tests covered here:

* DEADLINE-1 — when ``wall_clock_deadline`` is in the past (or < min threshold),
  the backstop returns an immediate fallback containing the tool-results digest
  and FORCED_ANSWER_TIMEOUT_MESSAGE, without calling the LLM for synthesis.
* DEADLINE-2 — when ``wall_clock_deadline`` is far enough in the future, the
  backstop proceeds normally (LLM call is made and the loop returns a real answer).
* DEADLINE-3 — when the deadline leaves only slightly more than the minimum but
  less than what the slow LLM needs, the backstop caps the LLM call timeout so
  it times out before the outer deadline, returning the explicit timeout fallback
  (rather than letting the outer ``wait_for`` cancel everything silently).
* DEADLINE-5 — the pre-synthesis compaction summariser honours a caller-supplied
  ``llm_timeout`` cap: a slow summariser degrades to the deterministic pruner
  within the cap instead of holding the wall-clock budget hostage (the exact
  residual race flagged in review: compaction, not synthesis, being the call
  the outer ``asyncio.wait_for`` cancels).
"""

from __future__ import annotations

import asyncio
import time

import pytest

# Single module-alias import (no parallel ``from app.tools.agent_loop
# import ...``) so monkeypatching module constants and referencing loop
# symbols share one import style (CodeQL py/import-and-import-from).
import app.tools.agent_loop as al
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_echo_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _echo(i: int = 0) -> str:
        return f"echo-result-payload-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [ToolDefinition(name="echo_tool", description="Echo a value back")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


class _FrozenMonotonic:
    """Stand-in for the ``time`` module as ``agent_loop`` sees it: ``monotonic``
    always answers *now*, every other attribute is the real module's.

    Only the loop's deadline arithmetic is frozen; asyncio's timeouts keep the
    real clock, so a deadline-capped call still times out in real seconds.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def monotonic(self) -> float:
        return self._now

    def __getattr__(self, name: str):
        return getattr(time, name)


class _BudgetBurnerLLM(BaseLLMProvider):
    """LLM that keeps calling a tool until the iteration budget is exhausted.

    On tool-stripped iterations (no tools offered) it returns empty text so the
    loop falls through to the forced-final-answer backstop.
    On forced-answer backstop calls (detected via the ``_FORCED_FINAL_ANSWER``
    system prompt marker) it records the call and returns a real answer.
    """

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
        # Per-call usage lets tests assert that the accumulated usage
        # survives every backstop exit path (including the immediate
        # deadline fallback, which must not drop run accounting).
        usage = {"total_tokens": 7}
        if any((m.content or "") == al._FORCED_FINAL_ANSWER for m in messages):
            self.forced_calls += 1
            return ChatResponse(
                content="Here is my final answer.", model=model or "stub", usage=usage
            )
        if tools:
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
                usage=usage,
            )
        # Tool-stripped iteration: empty so the loop falls to the backstop.
        return ChatResponse(content="", model=model or "stub", usage=usage)

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


# ---------------------------------------------------------------------------
# DEADLINE-1 — immediate fallback when deadline is expired / too close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_immediate_fallback_when_deadline_expired():
    """When ``wall_clock_deadline`` is in the past (remaining < 0), the loop
    breaks out of its first pass before issuing any LLM or tool call, and the
    forced-final-answer backstop returns an immediate pre-built fallback
    without calling the LLM for synthesis (DEADLINE-1).

    The budget guard at the top of the first loop pass
    fires before any work happens — ``_IN_LOOP_MIN_REMAINING_SECS`` is always
    positive and an already-elapsed deadline has negative remaining time, so
    the break is unconditional.
    """
    definitions, composite = _build_echo_executor()
    llm = _BudgetBurnerLLM()

    # A deadline already in the past ensures remaining < _FORCED_ANSWER_MIN_REMAINING_SECS.
    past_deadline = time.monotonic() - 1.0

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        wall_clock_deadline=past_deadline,
    )

    # No LLM calls at all — the loop guard breaks before any iteration, and
    # the backstop returns the immediate fallback (no synthesis call either).
    assert llm.call_count == 0, (
        "loop guard should prevent any LLM calls when the deadline is already past"
    )
    assert llm.forced_calls == 0, (
        "backstop should skip the LLM call and return immediately when the "
        "deadline has already elapsed"
    )
    # The response must contain the explicit timeout lead-in.
    assert al.FORCED_ANSWER_TIMEOUT_MESSAGE in response.content


@pytest.mark.asyncio
async def test_backstop_immediate_fallback_when_deadline_below_minimum(monkeypatch):
    """When the remaining budget is positive but below
    ``_FORCED_ANSWER_MIN_REMAINING_SECS``, the backstop still returns the
    immediate pre-built fallback instead of attempting a doomed LLM call
    (DEADLINE-1 variant)."""
    # Patch to small sentinel values so the test completes instantly.
    # _IN_LOOP_MIN_REMAINING_SECS is patched to 1 s so the 3-second budget
    # still allows iterations to run and populate echo-result-payload; the
    # backstop then fires with a remaining budget below the patched
    # _FORCED_ANSWER_MIN_REMAINING_SECS (5 s) and returns an immediate fallback.
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 1)
    monkeypatch.setattr(al, "_FORCED_ANSWER_MIN_REMAINING_SECS", 5)
    monkeypatch.setattr(al, "_FORCED_ANSWER_DEADLINE_RESERVE_SECS", 2)

    definitions, composite = _build_echo_executor()
    llm = _BudgetBurnerLLM()

    # 3 seconds from now → remaining < 5 s patched threshold → immediate fallback.
    deadline = time.monotonic() + 3.0

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    assert llm.forced_calls == 0, (
        "backstop should skip the LLM call and return immediately when "
        "remaining budget is below the minimum threshold"
    )
    assert al.FORCED_ANSWER_TIMEOUT_MESSAGE in response.content
    assert "echo-result-payload" in response.content
    # Token usage accumulated during the tool iterations must survive the
    # immediate-fallback shortcut so run accounting is not silently zeroed
    # (coverage moved here from the deadline-expired test, where the
    # loop guard now prevents any iterations from running at all).
    assert response.usage.get("total_tokens", 0) > 0


# ---------------------------------------------------------------------------
# DEADLINE-2 — normal operation when deadline is far away
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_proceeds_normally_when_deadline_is_far():
    """When ``wall_clock_deadline`` is far in the future, the forced-final-answer
    backstop proceeds normally — the LLM call is made and the loop returns the
    real answer (DEADLINE-2)."""
    definitions, composite = _build_echo_executor()
    llm = _BudgetBurnerLLM()

    # 120 seconds from now — well above any threshold.
    far_deadline = time.monotonic() + 120.0

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        wall_clock_deadline=far_deadline,
    )

    # The backstop LLM call must have been made.
    assert llm.forced_calls >= 1, "backstop should have called the LLM normally"
    assert "Here is my final answer." in response.content


# ---------------------------------------------------------------------------
# DEADLINE-3 — deadline-capped LLM timeout produces explicit timeout fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_caps_llm_timeout_to_remaining_budget(monkeypatch):
    """When the remaining budget is above the minimum threshold but the LLM call
    takes longer than ``remaining - reserve``, the backstop times out the call
    cleanly and returns ``FORCED_ANSWER_TIMEOUT_MESSAGE`` — the outer
    ``asyncio.wait_for`` never fires and the run record is not empty
    (DEADLINE-3).

    Monkeypatched small constants make this test run in about 2 seconds.

    The loop reads a frozen ``time.monotonic`` (``_FrozenMonotonic``): the
    backstop always sees exactly 3 s remaining, whatever the runner's load.
    A real clock let a 1 s runner stall before the first tool call drop the
    remaining budget under the 2 s minimum, so the backstop skipped the call
    (CI 41573309096, "assert 0 >= 1").
    """
    # Patch to small values: min=2s, reserve=1s  →  call timeout ≈ remaining − 1s.
    # _IN_LOOP_MIN_REMAINING_SECS is patched to 1 s so the 3-second budget
    # still allows tool iterations to run before the backstop fires.
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 1)
    monkeypatch.setattr(al, "_FORCED_ANSWER_MIN_REMAINING_SECS", 2)
    monkeypatch.setattr(al, "_FORCED_ANSWER_DEADLINE_RESERVE_SECS", 1)

    class _SlowForcedAnswerLLM(_BudgetBurnerLLM):
        """Overrides only the forced-answer call so it never answers: the
        deadline-capped call timeout of 2 s (3 s remaining on the frozen clock,
        minus the 1 s patched reserve; see the timeline comment on the
        ``deadline`` below) is the only thing that ends it."""

        async def chat(self, messages, model=None, tools=None, **kwargs):
            if any((m.content or "") == al._FORCED_FINAL_ANSWER for m in messages):
                self.forced_calls += 1
                # Never answer: only the deadline-capped timeout ends the call.
                await asyncio.Event().wait()
                return ChatResponse(content="forced answer", model=model or "stub")
            return await super().chat(messages, model=model, tools=tools, **kwargs)

    definitions, composite = _build_echo_executor()
    llm = _SlowForcedAnswerLLM()

    # Frozen clock, 3 s before the deadline at every reading:
    #   remaining = 3s ≥ 2s minimum → attempt LLM call.
    #   LLM call timeout = min(3 - 1, _LLM_CALL_TIMEOUT) = 2s (asyncio's own clock).
    #   Slow LLM never answers → times out at 2s → forced_call_failure = "timeout".
    #   Response must lead with FORCED_ANSWER_TIMEOUT_MESSAGE and include a digest.
    frozen_now = time.monotonic()
    deadline = frozen_now + 3.0
    monkeypatch.setattr(al, "time", _FrozenMonotonic(frozen_now))

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    assert llm.forced_calls >= 1, "backstop LLM call should have been attempted"
    assert al.FORCED_ANSWER_TIMEOUT_MESSAGE in response.content
    assert "Partial results:" in response.content
    assert "echo-result-payload" in response.content


# ---------------------------------------------------------------------------
# DEADLINE-4 — no wall_clock_deadline has zero behaviour change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_wall_clock_deadline_is_unchanged():
    """When ``wall_clock_deadline`` is omitted, the loop behaves exactly as
    before — no regression for callers that don't set the parameter."""
    definitions, composite = _build_echo_executor()
    llm = _BudgetBurnerLLM()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        # No wall_clock_deadline — legacy behaviour.
    )

    assert llm.forced_calls >= 1, "backstop should have called the LLM normally"
    assert "Here is my final answer." in response.content


# ---------------------------------------------------------------------------
# DEADLINE-5 — compaction summariser honours the caller-supplied timeout cap
# ---------------------------------------------------------------------------


class _SlowSummariserLLM(_BudgetBurnerLLM):
    """LLM whose every ``chat`` call sleeps far longer than the capped timeout,
    standing in for a summariser call that would blow the wall-clock budget."""

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.call_count += 1
        await asyncio.sleep(30.0)
        return ChatResponse(content="too late", model=model or "stub")


@pytest.mark.asyncio
async def test_summarise_messages_honours_llm_timeout_cap():
    """``_summarize_messages_for_context`` must abort a slow summariser call at
    the caller-supplied ``llm_timeout`` and return ``""`` (the miss contract)
    instead of holding the wall-clock budget for the full 900 s
    ``_LLM_CALL_TIMEOUT`` (DEADLINE-5)."""
    llm = _SlowSummariserLLM()
    middle = [
        ChatMessage(role="assistant", content=f"analysis chunk {i}: " + "x" * 500)
        for i in range(10)
    ]

    started = time.monotonic()
    summary = await al._summarize_messages_for_context(
        llm, middle, model=None, llm_timeout=0.3
    )
    elapsed = time.monotonic() - started

    assert summary == "", "timed-out summariser must return the empty-miss value"
    assert elapsed < 5.0, f"summariser should abort at ~0.3s, took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_compact_for_empty_recovery_honours_llm_timeout_cap():
    """``_compact_for_empty_recovery`` on a large context must pass the capped
    ``llm_timeout`` down to the semantic-compaction summariser and degrade to
    the deterministic pruner when it times out — returning well within the
    remaining wall-clock budget instead of racing the outer ``asyncio.wait_for``
    (DEADLINE-5)."""
    llm = _SlowSummariserLLM()
    # Large enough to cross _EMPTY_RESPONSE_RECOVERY_TRIGGER_CHARS (60k) and
    # give semantic compaction a summarisable middle (> 8k chars).
    filler = "tool output line " * 300  # ~5.1k chars each
    messages = [ChatMessage(role="user", content="original task: summarise the data")]
    for i in range(16):
        messages.append(
            ChatMessage(role="assistant", content=f"step {i}: {filler}")
        )
    before = sum(len(m.content or "") for m in messages)
    assert before > al._EMPTY_RESPONSE_RECOVERY_TRIGGER_CHARS

    started = time.monotonic()
    after = await al._compact_for_empty_recovery(
        llm,
        messages,
        model=None,
        max_session_chars=200_000,
        attempt=1,
        phase="forced-answer backstop",
        llm_timeout=0.3,
    )
    elapsed = time.monotonic() - started

    assert llm.call_count >= 1, "semantic compaction should have attempted the LLM"
    assert elapsed < 5.0, (
        f"compaction should abort the summariser at ~0.3s and fall back to the "
        f"deterministic pruner, took {elapsed:.1f}s"
    )
    assert after < before, "deterministic fallback should still shrink the context"


# ---------------------------------------------------------------------------
# DEADLINE-6 — per-iteration budget guard breaks to backstop before near-zero
#              timeout call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_skips_to_backstop_when_budget_near_exhausted(monkeypatch):
    """When the remaining wall-clock budget at iteration start drops below
    ``_IN_LOOP_MIN_REMAINING_SECS``, the loop breaks immediately to the
    forced-final-answer backstop instead of issuing a near-zero-timeout LLM
    call that always fails with "timed out after 1s" (DEADLINE-6).

    The stub LLM answers instantly, so the test completes in well under a
    second — the generous 30 s budget below only exists as timing slack.
    """
    # Raise the guard above the test budget so it trips on the very first
    # pass.  The backstop constants keep their production values: with
    # ~30 s remaining (>> _FORCED_ANSWER_MIN_REMAINING_SECS = 10) the
    # backstop takes the synthesis path and _BudgetBurnerLLM records
    # forced_calls.  The ~20 s margin between budget and backstop minimum
    # keeps this deterministic even on a slow, loaded CI runner.
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 60)

    definitions, composite = _build_echo_executor()
    llm = _BudgetBurnerLLM()

    # 30 s budget — below the patched 60 s guard but far above the (real)
    # 10 s backstop minimum, so: loop breaks immediately (no in-loop calls),
    # then the backstop makes one synthesis call and returns a real answer.
    deadline = time.monotonic() + 30.0

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    # All LLM calls must be forced-answer backstop calls — no regular in-loop
    # calls should have been attempted with a near-exhausted budget.
    assert llm.forced_calls >= 1, (
        "backstop should have called the LLM for synthesis after the loop guard break"
    )
    assert llm.call_count == llm.forced_calls, (
        "no in-loop LLM calls should have been made: only backstop calls expected"
    )
    assert "Here is my final answer." in response.content
    # Token usage from the backstop synthesis call must be carried through
    # to the response so run accounting is not silently zeroed on this path.
    assert response.usage.get("total_tokens", 0) > 0, (
        "backstop synthesis usage must survive into the final response"
    )

