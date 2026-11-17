"""Tests for ``select_best_chat_reply`` — the chat-side deliverable rescue.

These tests pin the bug where reasoning-forward models (observed with
``gpt-6-astra``) front-load the substantive answer BEFORE a side-effect tool
call (``store_memory``, …) and close with a one-liner after the tool result
comes back: the pre-tool text is routed into the reasoning pane by the
preamble hold-back, so the persisted chat reply was only the closer — the
user read "Stored." and never saw the answer.
"""

from dataclasses import dataclass, field
from typing import Any

from app.services.assignment_helpers import select_best_chat_reply


@dataclass
class _Msg:
    role: str
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bug path — the Astra "answer, then store_memory, then 'Stored.'" shape.
# ---------------------------------------------------------------------------


def test_front_loaded_answer_before_tool_call_is_rescued():
    """The pinned bug: the model writes the full answer, calls store_memory,
    then closes with a brief post-tool remark. The answer must survive into
    the reply."""
    answer = (
        "Here it is, straight from my config:\n\n"
        "Model: gpt-6-astra (OpenAI Astra)\n"
        "Provider: auto-resolved\n"
        "Temperature: 0.9\n"
        "Reasoning effort: default\n"
        "Max iterations: 100\n"
        "Memory: all four tiers on\n"
        "HITL: off\n"
        "Config last updated: 2026-09-05 08:03 UTC\n\n"
        "So you were right and I was wrong twice over: the tools existed, "
        "and the model is Astra, not Claude."
    )
    closer = "Stored both facts so I don't relitigate this next session."
    run_messages = [
        _Msg(role="assistant", content=answer, tool_calls=[object()]),
        _Msg(role="tool", content="Memory stored."),
        _Msg(role="assistant", content=closer),
    ]

    result = select_best_chat_reply(closer, run_messages)

    assert answer in result
    assert closer in result


def test_empty_final_falls_back_to_earlier_deliverable():
    """A run that ends with an empty final turn surfaces the earlier
    substantive message instead of nothing."""
    answer = "The quarterly numbers you asked for: " + ("revenue line. " * 30)
    run_messages = [
        _Msg(role="assistant", content=answer, tool_calls=[object()]),
        _Msg(role="tool", content="ok"),
        _Msg(role="assistant", content=""),
    ]

    assert select_best_chat_reply("", run_messages) == answer


# ---------------------------------------------------------------------------
# Healthy paths — no rescue needed.
# ---------------------------------------------------------------------------


def test_final_answer_is_longest_passes_through():
    """When the final turn IS the deliverable, the reply is unchanged."""
    preamble = "Let me pull your configuration first."
    final = "Full answer with all the details. " * 40
    run_messages = [
        _Msg(role="assistant", content=preamble, tool_calls=[object()]),
        _Msg(role="tool", content="config payload"),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_chat_reply(final, run_messages) == final


def test_short_preamble_never_overrides_short_final():
    """A genuine short preamble below the substantial threshold must not be
    merged into a short final answer."""
    preamble = "Checking the platform config now."
    final = "It's gpt-6-astra."
    run_messages = [
        _Msg(role="assistant", content=preamble, tool_calls=[object()]),
        _Msg(role="tool", content="config payload"),
        _Msg(role="assistant", content=final),
    ]

    assert select_best_chat_reply(final, run_messages) == final


# ---------------------------------------------------------------------------
# Segment exclusion — mid-turn deliverables already shown as real messages
# must never be duplicated into the final reply.
# ---------------------------------------------------------------------------


def test_delivered_segment_is_not_duplicated():
    """A mid-turn report already persisted via segment_complete is excluded
    from the rescue candidates."""
    segment = "Mid-turn status report the user already read. " * 20
    closer = "Both delegations finished; nothing else pending."
    run_messages = [
        _Msg(role="assistant", content=segment, tool_calls=[object()]),
        _Msg(role="tool", content="wait result"),
        _Msg(role="assistant", content=closer),
    ]

    result = select_best_chat_reply(
        closer, run_messages, delivered_segments=[segment]
    )

    assert result == closer


def test_segment_delivered_from_a_tool_round_is_not_duplicated():
    """The loop now delivers a flushed answer followed by a side-effect
    tool call (``store_memory``) as a segment.  The transcript still holds
    that text on the tool-calling assistant turn; the rescue must exclude it
    so the final reply is only the closer (the segment is already on screen
    as its own message)."""
    answer = "Here is the answer, straight from my config: " + ("detail. " * 40)
    closer = "Stored both facts so I don't relitigate this next session."
    run_messages = [
        _Msg(role="assistant", content=answer, tool_calls=[object()]),
        _Msg(role="tool", content="Memory stored."),
        _Msg(role="assistant", content=closer),
    ]

    result = select_best_chat_reply(
        closer, run_messages, delivered_segments=[answer]
    )

    assert result == closer


def test_non_delivered_sibling_still_rescued_alongside_delivered_segment():
    """Only the exact delivered texts are excluded — a different substantive
    turn in the same run is still rescued."""
    segment = "Mid-turn status report the user already read. " * 20
    answer = "The actual final deliverable content. " * 20
    closer = "Done."
    run_messages = [
        _Msg(role="assistant", content=segment, tool_calls=[object()]),
        _Msg(role="tool", content="wait result"),
        _Msg(role="assistant", content=answer, tool_calls=[object()]),
        _Msg(role="tool", content="ok"),
        _Msg(role="assistant", content=closer),
    ]

    result = select_best_chat_reply(
        closer, run_messages, delivered_segments=[segment]
    )

    assert answer in result
    assert segment not in result


def test_blank_delivered_segments_are_ignored():
    """Whitespace-only entries in delivered_segments must not exclude
    anything (and must not crash)."""
    answer = "Substantive deliverable content. " * 20
    closer = "Wrapped up."
    run_messages = [
        _Msg(role="assistant", content=answer, tool_calls=[object()]),
        _Msg(role="tool", content="ok"),
        _Msg(role="assistant", content=closer),
    ]

    result = select_best_chat_reply(
        closer, run_messages, delivered_segments=["", "   "]
    )

    assert answer in result
