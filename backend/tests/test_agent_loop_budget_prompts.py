"""Tests for the budget-aware guidance prompts in the agent loop.

The static ``_WIND_DOWN_NUDGE`` / ``_TRUNCATED_TOOL_CALL_RETRY`` strings
were replaced with dynamic builders that inject the current iteration
budget into the system message.  These tests pin both:

* presence of the explicit budget numbers in the rendered prompt (so a
  refactor that drops the f-string back to a static template trips
  here), and
* presence of the checkpoint / completion-summary guidance shared with
  :data:`_CONTINUATION_PROMPT` and ``interrupted_turn_framing`` so the
  three truncation-recovery surfaces stay consistent.
"""

from app.tools.agent_loop import (
    _build_truncated_tool_call_retry,
    _build_wind_down_nudge,
)


def test_wind_down_nudge_includes_remaining_budget_and_completion_guidance():
    """Wind-down nudge MUST include the dynamic budget numbers AND the
    completion-report demand.  A regression to the old static template
    (which only said "approaching the maximum" with no concrete count)
    trips both assertions — the model loses the actionable budget plan
    that produces tighter completion summaries.
    """
    prompt = _build_wind_down_nudge(remaining_iterations=3, max_iterations=12)
    assert "3 iteration(s) left out of 12" in prompt
    assert "including this one" in prompt, (
        "the budget count MUST clarify it includes the current iteration so "
        "the LLM doesn't double-count and stop one iteration too early"
    )
    assert "reserve enough response budget" in prompt
    assert "final completion report" in prompt


def test_wind_down_nudge_handles_single_iteration_remaining():
    """Boundary case: 1 iteration remaining.  Uses the same ``(s)``
    template — the wording stays grammatically a bit loose but is
    clearer than maintaining two parallel templates for a single
    character.  Pin the rendered form so a refactor that introduced
    conditional pluralisation doesn't accidentally drop the count.
    """
    prompt = _build_wind_down_nudge(remaining_iterations=1, max_iterations=8)
    assert "1 iteration(s) left out of 8" in prompt


def test_truncated_retry_includes_budget_and_checkpoint_guidance():
    """Truncated-tool-call retry MUST include the dynamic remaining
    budget AND ask for a checkpoint-style status.  Without the budget
    context, the LLM tends to keep retrying the same tool call even
    when only 1-2 iterations remain — burning the budget on a tool that
    cannot fit instead of synthesising what it already has.
    """
    prompt = _build_truncated_tool_call_retry(remaining_iterations=2)
    assert "only 2 iteration(s) remain" in prompt
    assert "checkpoint-style status" in prompt
    assert "final completion summary" in prompt


def test_truncated_retry_does_not_force_retry():
    """The retry prompt MUST keep the "answer without the tool" branch
    as the recommended path.  A regression that hardcoded "retry the
    tool" would burn the budget on tool calls the model couldn't fit
    in the first place.
    """
    prompt = _build_truncated_tool_call_retry(remaining_iterations=4)
    assert "can answer the user WITHOUT calling this tool" in prompt
    assert "If the tool call is absolutely essential" in prompt


def test_truncated_retry_caller_uses_post_continue_budget_count():
    """Static guard on the off-by-one fix in the truncation-retry caller.

    The truncated-tool-call recovery in ``agent_loop.py`` consumes the
    current iteration and ``continue``s; the system message it appends
    is read by the LLM on iteration ``N + 1``, so the post-``continue``
    count is ``max_iterations - iteration - 1``, NOT
    ``max_iterations - iteration``.  Without the ``- 1`` the prompt
    overstates the budget by one and the LLM keeps trying tools
    believing it has more room than it actually does.

    This is a static-source guard rather than a full agent-loop
    integration test because the truncation path inside
    ``run_agent_loop`` requires a streaming-LLM + tool-call fixture
    that the rest of the file deliberately avoids — the format
    function above pins the prompt shape, and this test pins the
    caller's computation against a regression to the pre-``continue``
    formula.

    The asymmetry with the wind-down nudge ("- 1" here, no "- 1"
    there) is intentional and called out in the inline comment block
    above the ``_remaining`` assignment.  This test asserts BOTH that
    the truncation site uses ``- 1`` AND that the wind-down site does
    not, so a "consistency" refactor that aligns the two sites in
    either direction trips the right assertion with a precise error.
    """
    import inspect

    from app.tools import agent_loop

    source = inspect.getsource(agent_loop.run_agent_loop)
    # Truncation site: ``max_iterations - iteration - 1`` (or the
    # ``max(0, ...)`` wrapper) — required so the prompt reports the
    # post-``continue`` count the LLM actually has.
    assert "max_iterations - iteration - 1" in source, (
        "the truncated-tool-call retry site MUST compute "
        "``_remaining = max_iterations - iteration - 1`` (post-``continue`` "
        "count) — without the ``- 1`` the prompt overstates the budget by "
        "one and the LLM keeps retrying tools believing it has more room "
        "than it actually does"
    )
    # Wind-down site: ``max_iterations - iteration`` (no ``- 1``) —
    # the nudge runs BEFORE the LLM call for the current iteration so
    # "(N left ... including this one)" is honest.  Pin the asymmetry
    # so a future "make these consistent" refactor doesn't accidentally
    # align them in the wrong direction.
    assert "_remaining = max_iterations - iteration\n" in source, (
        "the wind-down nudge MUST compute "
        "``_remaining = max_iterations - iteration`` (no ``- 1``) — the "
        "nudge runs BEFORE the LLM call for the current iteration and the "
        "prompt text correctly says 'including this one', so subtracting "
        "again would undercount the budget by one"
    )
