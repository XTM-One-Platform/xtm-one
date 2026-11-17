"""Tests for ``select_best_result_summary`` — the helper that picks the most
informative assistant content for ``AssignmentRun.result_summary``.

These tests pin the bug where the Result card and notification surfaced
"Your daily digest is ready above!" instead of the digest itself: the LLM
emitted the substantive deliverable in an earlier iteration (combined with
a tool call) and ended the loop with a brief recap, so capturing only the
final assistant message lost the content.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services.assignment_helpers import select_best_result_summary
from app.tools.agent_loop import (
    _LEAKED_TOOL_CALL_NUDGE,
    _STALL_CONTINUATION_NUDGE,
)


@dataclass
class _Msg:
    role: str
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Healthy paths — the final message IS the deliverable.
# ---------------------------------------------------------------------------


def test_single_assistant_message_returned_as_is():
    """One-shot answers (no tool calls) must pass through unchanged."""
    final = "Hello, here is your digest with three priorities."
    messages = [
        _Msg(role="system", content="You are an agent."),
        _Msg(role="user", content="Generate my digest"),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


def test_final_is_longest_returned_as_is():
    """When the final answer is the longest substantive message we keep it
    verbatim — combining would duplicate content."""
    earlier = "Let me fetch your emails first."
    final = "📬 Inbox Digest\n\n" + ("Full digest body. " * 200)
    messages = [
        _Msg(role="user", content="Generate my digest"),
        _Msg(role="assistant", content=earlier, tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


def test_short_earlier_below_threshold_is_ignored():
    """Earlier assistant turns shorter than the substantial threshold (e.g.
    "Let me check your inbox.") must never override the final answer."""
    earlier = "Let me check your inbox."
    final = "Quick reply done."  # short final, but earlier is below threshold
    messages = [
        _Msg(role="assistant", content=earlier, tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


# ---------------------------------------------------------------------------
# Bug path — final message is a brief recap of an earlier substantive turn.
# ---------------------------------------------------------------------------


def test_brief_recap_after_long_deliverable_combines_both():
    """The pinned bug: final message is "Your digest is ready above!" while
    the deliverable was emitted earlier alongside a tool call."""
    digest = "📬 Inbox Digest — May 26\n\n" + ("Email summary line. " * 200)
    recap = (
        "Your daily digest is ready above! The most pressing items today:\n"
        "1. Approve Pennylane invoices\n2. Reply to Mickos\n3. Input to Floriane"
    )
    messages = [
        _Msg(role="user", content="Generate my digest"),
        _Msg(role="assistant", content="Let me fetch.", tool_calls=[object()]),
        _Msg(role="assistant", content=digest, tool_calls=[object()]),
        _Msg(role="assistant", content=recap),
    ]

    result = select_best_result_summary(recap, messages)

    assert digest in result, "deliverable must be preserved"
    assert recap in result, "wrap-up info must be preserved"
    assert result.startswith(digest), "deliverable should come first"
    assert "\n\n---\n\n" in result, "separator should split the two parts"


def test_recap_at_exact_short_ratio_threshold_combines():
    """Boundary check: a final message at exactly the recap ratio (50% of
    the earlier substantive turn) must trigger combination."""
    earlier = "A" * 1000
    final = "B" * 500  # exactly 50% — boundary inclusive
    messages = [
        _Msg(role="assistant", content=earlier, tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    result = select_best_result_summary(final, messages)

    assert earlier in result
    assert final in result


def test_comparable_lengths_do_not_combine():
    """When the final answer is in the same ballpark as earlier turns (e.g.
    a wind-down forced summary), we keep the final verbatim — combining
    would just create near-duplicate content."""
    earlier = "Deliverable A. " * 100  # ~1500 chars
    final = "Comprehensive summary covering everything. " * 80  # ~3500 chars
    messages = [
        _Msg(role="assistant", content=earlier, tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_empty_final_falls_back_to_longest_earlier():
    """If the loop ended with an empty final answer (rare, but happens on
    forced-final-answer failures), surface the longest substantive earlier
    turn rather than an empty Result card."""
    digest = "📬 Inbox Digest body " * 50
    messages = [
        _Msg(role="assistant", content="Fetching…", tool_calls=[object()]),
        _Msg(role="assistant", content=digest, tool_calls=[object()]),
        _Msg(role="assistant", content=""),
    ]

    assert select_best_result_summary("", messages) == digest


def test_no_assistant_messages_returns_final_unchanged():
    """Defensive: pre-loop failure with no assistant turns must not blow
    up and must return whatever ``final_content`` carries (possibly empty)."""
    messages = [
        _Msg(role="system", content="System prompt"),
        _Msg(role="user", content="The user message"),
    ]

    assert select_best_result_summary("fallback", messages) == "fallback"
    assert select_best_result_summary(None, messages) == ""


def test_none_final_with_substantive_earlier():
    """``loop_result.content`` can be ``None`` after a provider hiccup. The
    earlier deliverable must still be surfaced."""
    digest = "Email digest line. " * 50
    messages = [
        _Msg(role="assistant", content=digest, tool_calls=[object()]),
        _Msg(role="assistant", content=None),
    ]

    assert select_best_result_summary(None, messages) == digest


def test_whitespace_only_messages_are_ignored():
    """Whitespace-only assistant turns must not be considered candidates."""
    final = "Real final answer."
    messages = [
        _Msg(role="assistant", content="   \n  ", tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


def test_tool_and_system_messages_are_ignored():
    """Only assistant messages are considered — tool results and system
    nudges must never leak into ``result_summary``."""
    final = "The final answer."
    messages = [
        _Msg(role="system", content="A very long system prompt " * 100),
        _Msg(role="user", content="Question"),
        _Msg(role="tool", content="Huge tool result blob " * 200),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_result_summary(final, messages) == final


def test_logs_when_combining(caplog: pytest.LogCaptureFixture):
    """Combination must emit an INFO log so operators can spot recap-style
    LLM behaviour in production traces."""
    earlier = "Long deliverable. " * 50
    final = "Short recap."
    messages = [
        _Msg(role="assistant", content=earlier, tool_calls=[object()]),
        _Msg(role="assistant", content=final),
    ]

    with caplog.at_level("INFO", logger="app.services.assignment_helpers"):
        select_best_result_summary(final, messages)

    assert any("brief recap" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Stall-guard narration must never leak into the result summary.
# ---------------------------------------------------------------------------


def test_stall_narration_is_excluded_from_result_summary():
    """The agent loop's stall guard appends a retracted "about to act"
    narration (immediately followed by its execute-now system nudge) to the
    working transcript for continuity. Even when that narration is the longest
    earlier substantive assistant turn and the real final answer is short, it
    must NOT be combined into the user-facing result - it is a retracted
    checkpoint, not the deliverable."""
    narration = (
        "Done analyzing the control set. Now let me write the records and "
        "assign the owners to every in-scope control in one bulk call. "
    ) * 4  # comfortably over the substantial threshold and longer than `final`
    final = "All 425 owners assigned. Task complete."
    messages = [
        _Msg(role="user", content="Assign all owners"),
        _Msg(role="assistant", content="", tool_calls=[object()]),
        _Msg(role="assistant", content=narration),
        _Msg(role="system", content=_STALL_CONTINUATION_NUDGE),
        _Msg(role="assistant", content=final),
    ]

    result = select_best_result_summary(final, messages)

    assert result == final, "the retracted narration must not be combined in"
    assert "Now let me write the records" not in result


def test_leaked_tool_call_markup_is_excluded_from_result_summary():
    """The leaked-tool-call guard keeps the markup the model emitted as text in
    the working transcript (immediately followed by its system nudge) so the
    next turn can invoke the tool for real. That checkpoint is a dropped call,
    not a deliverable: when the retry runs the tool and the run ends with a
    short "Done.", the markup must NOT be surfaced or combined into the result."""
    leaked = (
        "Now I have the data. Let me create the scenario.\n\n"
        '<function_calls>\n<invoke name="create_openaev_scenario">\n'
        '<parameter name="name">Probe</parameter>\n</invoke>\n</function_calls>\n'
    ) * 2  # over the substantial threshold and longer than `final`
    final = "Done. Scenario created."
    messages = [
        _Msg(role="user", content="Build a scenario"),
        _Msg(role="assistant", content="", tool_calls=[object()]),
        _Msg(role="assistant", content=leaked),
        _Msg(role="system", content=_LEAKED_TOOL_CALL_NUDGE),
        _Msg(role="assistant", content=final),
    ]

    result = select_best_result_summary(final, messages)

    assert result == final, "the leaked markup must not be surfaced as the result"
    assert "function_calls" not in result


def test_real_deliverable_still_combined_when_a_stall_narration_is_present():
    """Excluding the stall narration must not suppress a genuine earlier
    deliverable: a real digest emitted alongside a tool call is still combined
    with a short final recap, while the retracted narration is dropped."""
    narration = "Now let me pull the dataset and write the digest at scale. " * 4
    digest = "Inbox Digest\n\n" + ("Email summary line. " * 200)
    recap = "Your digest is ready above! Top items: 1, 2, 3."
    messages = [
        _Msg(role="user", content="Generate my digest"),
        _Msg(role="assistant", content=narration),
        _Msg(role="system", content=_STALL_CONTINUATION_NUDGE),
        _Msg(role="assistant", content=digest, tool_calls=[object()]),
        _Msg(role="assistant", content=recap),
    ]

    result = select_best_result_summary(recap, messages)

    assert digest in result, "real deliverable must still be preserved"
    assert recap in result, "wrap-up info must still be preserved"
    assert narration.strip() not in result, "narration must be excluded"
