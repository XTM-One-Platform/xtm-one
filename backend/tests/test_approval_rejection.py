"""Applying approval decisions when a gated run resumes.

``_apply_approval_decisions`` turns a reviewer's verdicts into tool results.
Two properties matter most:

* **Completeness.** Exactly one tool message per proposal, in call order. The
  conversation holds an assistant message with N ``tool_use`` blocks and
  strict-mode Anthropic/OpenAI reject it unless every one has a matching
  ``tool_result`` — so a skipped proposal makes the run unresumable, which is
  the same failure the batch pre-screen exists to prevent.
* **Rejection is structured, not an error.** The agent must be able to read the
  refusal, understand the action was declined, and adapt — rather than treat it
  as a tool malfunction and retry the same call.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.assignment_engine import (
    _apply_approval_decisions,
    _dispatch_calls_the_pause_left_behind,
)
from app.tools.approval import (
    OUTCOME_CLEARED_PRE_PAUSE,
    OUTCOME_NOT_OFFERED,
    OUTCOME_WHITELISTED,
)


class _FakeExecutor:
    def __init__(self, fail: bool = False):
        self.calls = []
        self._fail = fail

    async def execute_tool(self, call):
        self.calls.append(call)
        if self._fail:
            raise RuntimeError("upstream exploded")
        return SimpleNamespace(content=f"{call.name} ok", is_error=False)


def _proposal(call_id, tool_name="delete_entity", arguments=None):
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "arguments": arguments or {"target": "old"},
        "input_schema": {},
        "source": "test",
    }


async def _apply(proposals, decisions, executor=None):
    messages: list = []
    executor = executor or _FakeExecutor()
    await _apply_approval_decisions(
        executor=executor,
        messages=messages,
        proposals=proposals,
        decisions=decisions,
    )
    return messages, executor


async def test_approved_call_executes_with_original_arguments():
    messages, executor = await _apply(
        [_proposal("c1")], {"c1": {"decision": "approve"}}
    )

    assert [c.name for c in executor.calls] == ["delete_entity"]
    assert executor.calls[0].arguments == {"target": "old"}
    assert messages[0].tool_call_id == "c1"


async def test_an_approval_cannot_alter_what_was_proposed():
    """A decision answers the call as put. Even if a caller smuggles arguments
    into the decision, what executes is what the reviewer was shown — otherwise
    the transcript would credit the agent with a call it never made."""
    messages, executor = await _apply(
        [_proposal("c1")],
        {"c1": {"decision": "approve", "modified_arguments": {"target": "new"}}},
    )

    assert executor.calls[0].arguments == {"target": "old"}
    assert len(messages) == 1


async def test_rejected_call_does_not_execute():
    messages, executor = await _apply(
        [_proposal("c1")],
        {"c1": {"decision": "reject", "rejection_reason": "Wrong environment"}},
    )

    assert executor.calls == [], "a rejected call was executed"
    assert len(messages) == 1
    assert messages[0].tool_call_id == "c1"


async def test_rejection_is_readable_and_discourages_a_blind_retry():
    messages, _ = await _apply(
        [_proposal("c1")],
        {"c1": {"decision": "reject", "rejection_reason": "Wrong environment"}},
    )

    content = messages[0].content
    assert "Rejected by human" in content
    assert "Wrong environment" in content
    assert "not executed" in content
    assert "retry" in content.lower()


async def test_rejection_without_a_reason_still_reads_sensibly():
    messages, _ = await _apply([_proposal("c1")], {"c1": {"decision": "reject"}})

    assert "No reason given" in messages[0].content


async def test_every_proposal_gets_exactly_one_result():
    """Completeness — a missing result leaves an orphaned tool_use block."""
    proposals = [_proposal("c1"), _proposal("c2"), _proposal("c3")]
    decisions = {
        "c1": {"decision": "approve"},
        "c2": {"decision": "reject"},
        "c3": {"decision": "approve_always"},
    }

    messages, _ = await _apply(proposals, decisions)

    assert [m.tool_call_id for m in messages] == ["c1", "c2", "c3"]


async def test_results_follow_proposal_order():
    proposals = [_proposal("c1"), _proposal("c2")]
    messages, _ = await _apply(
        proposals, {"c2": {"decision": "approve"}, "c1": {"decision": "approve"}}
    )

    assert [m.tool_call_id for m in messages] == ["c1", "c2"]


async def test_missing_decision_defaults_to_reject():
    """Fail closed. A decision that never arrived must not become an execution."""
    messages, executor = await _apply([_proposal("c1")], {})

    assert executor.calls == []
    # Refused by default, and never credited to a person who gave no verdict.
    assert "Rejected by human" not in messages[0].content
    assert "No person rejected it" in messages[0].content


async def test_approve_always_executes_like_approve():
    _messages, executor = await _apply(
        [_proposal("c1")], {"c1": {"decision": "approve_always"}}
    )

    assert [c.name for c in executor.calls] == ["delete_entity"]


async def test_a_failing_approved_tool_still_yields_a_result():
    """One tool blowing up must not abandon the resume.

    The remaining proposals still need results, or the conversation is left
    invalid and the whole run becomes unresumable over a single tool error.
    """
    proposals = [_proposal("c1"), _proposal("c2")]
    decisions = {"c1": {"decision": "approve"}, "c2": {"decision": "approve"}}

    messages, _ = await _apply(proposals, decisions, executor=_FakeExecutor(fail=True))

    assert [m.tool_call_id for m in messages] == ["c1", "c2"]
    assert "Tool execution failed" in messages[0].content


# --- An approval is not a licence to run a tool that is no longer offered ----


async def test_the_dispatch_helper_runs_nothing_without_an_executor():
    """The deny-list can empty the offered surface while a reviewer thinks.

    The run path used to hand the bare composite here, which keeps executors
    registered for denied tool names — and this helper bypasses the
    gate before dispatching, so a deny-listed call ran unchecked. With no
    executor the calls still need a result each, or the resumed conversation is
    invalid.
    """
    messages: list = [
        SimpleNamespace(
            role="assistant",
            tool_calls=[
                SimpleNamespace(id="c1", name="delete_entity", arguments={}),
                SimpleNamespace(id="c2", name="send_email", arguments={}),
            ],
        )
    ]
    outcomes: dict[str, str] = {}

    await _dispatch_calls_the_pause_left_behind(
        executor=None, messages=messages, outcomes=outcomes
    )

    results = [m for m in messages if getattr(m, "role", None) == "tool"]
    assert [m.tool_call_id for m in results] == ["c1", "c2"]
    assert all("no longer offers that tool" in m.content for m in results)


async def test_a_rejected_verdict_never_reaches_the_executor():
    """What the run path now sends when the offered surface is empty: every
    verdict rewritten to ``reject``, so the applier answers without executing.
    """
    messages, executor = await _apply(
        [_proposal("c1")],
        {"c1": {"decision": "reject", "rejection_reason": "no longer offered"}},
    )

    assert executor.calls == []
    assert "no longer offered" in messages[0].content


def test_the_run_resume_never_falls_back_to_the_unscoped_composite():
    """Pins the call site, because the defect was there rather than in either
    helper: ``... if effective_tools else composite`` handed the resume an
    executor that still answers to denied tool names. Both chat paths already
    pass ``None`` and reject; the run path has to match.
    """
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    resume = source.split("_resume_decisions is not None")[1][:2000]
    assert "else composite" not in resume, (
        "the resume executor must be None when the offered surface is empty — "
        "the composite keeps denied executors registered"
    )
    assert "else None" in resume


# --- The trail must not say a call auto-ran when it did not ----------------


async def test_a_call_that_was_not_run_is_not_recorded_as_auto_run():
    """``auto_run_whitelisted`` claims two things — it ran, and a whitelist is
    why. On the no-executor branch neither is true: the call is answered with a
    refusal because the tool is no longer offered. FR-020 is about being able to
    answer "why did this run without asking me?" from the trail alone, and a
    wrong reason is worse than a missing one.
    """
    messages: list = [
        SimpleNamespace(
            role="assistant",
            tool_calls=[SimpleNamespace(id="c1", name="delete_entity", arguments={})],
        )
    ]
    outcomes: dict[str, str] = {}

    await _dispatch_calls_the_pause_left_behind(
        executor=None, messages=messages, outcomes=outcomes
    )

    assert outcomes["c1"] == OUTCOME_NOT_OFFERED
    assert outcomes["c1"] != OUTCOME_WHITELISTED


async def test_a_call_cleared_before_the_pause_does_not_invent_a_reason():
    """It really does run, un-gated, because the gate cleared it before the
    pause — but the outcome sink is fresh after the pause unwinds, so *which*
    of whitelist / integration exemption / standing approval cleared it is not
    known here. Naming that is honest; picking one is not.
    """
    messages: list = [
        SimpleNamespace(
            role="assistant",
            tool_calls=[SimpleNamespace(id="c1", name="get_entity", arguments={})],
        )
    ]
    outcomes: dict[str, str] = {}

    await _dispatch_calls_the_pause_left_behind(
        executor=_FakeExecutor(), messages=messages, outcomes=outcomes
    )

    assert outcomes["c1"] == OUTCOME_CLEARED_PRE_PAUSE


# --- a stopped turn runs nothing more --------------------------------


class _HangingFirstExecutor:
    """The first call hangs until cancelled; every call is recorded."""

    def __init__(self) -> None:
        import asyncio

        self.calls: list = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def execute_tool(self, call):
        import asyncio

        self.calls.append(call.name)
        if len(self.calls) == 1:
            self.started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return SimpleNamespace(content=f"{call.name} ok", is_error=False)


async def test_a_stopped_turn_runs_no_approved_call():
    import asyncio

    stopped = asyncio.Event()
    stopped.set()
    executor = _FakeExecutor()
    messages: list = []

    await _apply_approval_decisions(
        executor=executor,
        messages=messages,
        proposals=[_proposal("c1"), _proposal("c2", "send_email")],
        decisions={"c1": {"decision": "approve"}, "c2": {"decision": "approve"}},
        cancel_event=stopped,
    )

    assert executor.calls == []
    assert [(m.tool_call_id, m.content) for m in messages] == [
        ("c1", "The delete_entity call was not run: the turn was stopped."),
        ("c2", "The send_email call was not run: the turn was stopped."),
    ]


async def test_a_stop_during_an_approved_call_cancels_it_and_runs_no_other():
    import asyncio

    stop = asyncio.Event()
    executor = _HangingFirstExecutor()
    messages: list = []

    async def _stop_once_running():
        await executor.started.wait()
        stop.set()

    stopper = asyncio.create_task(_stop_once_running())
    await asyncio.wait_for(
        _apply_approval_decisions(
            executor=executor,
            messages=messages,
            proposals=[_proposal("c1"), _proposal("c2", "send_email")],
            decisions={"c1": {"decision": "approve"}, "c2": {"decision": "approve"}},
            cancel_event=stop,
        ),
        timeout=5,
    )
    await stopper

    assert executor.calls == ["delete_entity"]
    assert executor.cancelled is True
    assert [(m.tool_call_id, m.content) for m in messages] == [
        (
            "c1",
            "The delete_entity call was stopped while it ran: the turn was cancelled.",
        ),
        ("c2", "The send_email call was not run: the turn was stopped."),
    ]


async def test_a_stopped_resume_runs_none_of_the_calls_the_pause_left_behind():
    import asyncio

    stopped = asyncio.Event()
    stopped.set()
    executor = _FakeExecutor()
    messages: list = [
        SimpleNamespace(
            role="assistant",
            tool_calls=[SimpleNamespace(id="c1", name="read_inbox", arguments={})],
        )
    ]

    await _dispatch_calls_the_pause_left_behind(
        executor=executor, messages=messages, cancel_event=stopped
    )

    assert executor.calls == []
    results = [m for m in messages if getattr(m, "role", None) == "tool"]
    assert [(m.tool_call_id, m.content) for m in results] == [
        ("c1", "The read_inbox call was not run: the turn was stopped."),
    ]
