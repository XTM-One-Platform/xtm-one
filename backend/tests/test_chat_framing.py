"""Test recency-positioned framing messages for chat services.

This is the single source of truth for the gating logic that decides
which ``[…]`` system messages get injected between conversation history
and the new user message.  ``general_chat`` and ``agent_chat`` both
delegate to ``build_turn_framings`` so any divergence is caught here.
"""

import pytest

from app.services.chat_framing import (
    build_turn_framings,
    channel_thread_framing,
    current_turn_attachment_framing,
    current_turn_framing,
    interrupted_turn_framing,
    is_continue_request,
    message_with_briefs,
)

# ── Individual framing builders ─────────────────────────────────────


def test_channel_thread_framing_contents():
    msg = channel_thread_framing()
    assert msg.role == "system"
    assert "[Channel Thread]" in msg.content
    assert "public channel" in msg.content
    # Must steer away from unrelated topics so the agent stays on topic.
    assert "Do NOT" in msg.content


def test_current_turn_attachment_framing_contents():
    msg = current_turn_attachment_framing()
    assert msg.role == "system"
    assert "[Current Turn — New Attachment(s)]" in msg.content
    # Covers both files AND inline images so the framing applies even
    # to image-only turns where ``Available Files`` has nothing fresh.
    assert "file/image/screenshot" in msg.content
    # Deictic resolution guidance — the whole point of this framing.
    assert '"this"' in msg.content
    assert '"the file"' in msg.content
    assert '"the screenshot"' in msg.content
    # User-correction obedience.
    assert "obey the correction immediately" in msg.content


def test_current_turn_framing_contents():
    msg = current_turn_framing()
    assert msg.role == "system"
    assert "[Current Turn]" in msg.content
    # Generic precedence reminder — must not mention attachments
    # specifically (that's what the dedicated framing is for).
    assert "attachment" not in msg.content.lower()
    assert "precedence" in msg.content


# ── Gating logic — short conversations ──────────────────────────────


def test_no_framings_for_short_history_without_attachments():
    """A 2-message history with no attachments needs no framing."""
    framings = build_turn_framings(
        history_len=2,
        has_current_attachments=False,
    )
    assert framings == []


def test_no_framings_for_empty_history_without_attachments():
    """First-message-in-conversation case — no framing needed."""
    framings = build_turn_framings(
        history_len=0,
        has_current_attachments=False,
    )
    assert framings == []


def test_no_framings_for_one_message_history_without_attachments():
    framings = build_turn_framings(
        history_len=1,
        has_current_attachments=False,
    )
    assert framings == []


# ── Gating logic — generic [Current Turn] reminder ──────────────────


def test_generic_framing_fires_only_when_history_long_enough():
    """The generic reminder kicks in at history_len > 2."""
    short = build_turn_framings(history_len=2, has_current_attachments=False)
    long = build_turn_framings(history_len=3, has_current_attachments=False)
    assert short == []
    assert len(long) == 1
    assert "[Current Turn]" in long[0].content
    assert "[Current Turn — New Attachment(s)]" not in long[0].content


# ── Gating logic — attachment-precedence framing ────────────────────


def test_attachment_framing_fires_when_history_has_at_least_2_messages():
    """The attachment framing triggers earlier than the generic one."""
    framings = build_turn_framings(
        history_len=2,
        has_current_attachments=True,
    )
    # At history_len=2: attachment framing fires, generic does not.
    assert len(framings) == 1
    assert "[Current Turn — New Attachment(s)]" in framings[0].content


def test_attachment_framing_does_not_fire_for_first_message():
    """No need to frame attachments when there is nothing prior to confuse them with."""
    framings = build_turn_framings(
        history_len=0,
        has_current_attachments=True,
    )
    assert framings == []


def test_attachment_and_generic_framing_stack_in_long_conversation():
    """Both framings fire in a long conversation with new attachments."""
    framings = build_turn_framings(
        history_len=10,
        has_current_attachments=True,
    )
    assert len(framings) == 2
    # Attachment framing must come BEFORE the generic one so the LLM
    # processes the more specific guidance first.
    assert "[Current Turn — New Attachment(s)]" in framings[0].content
    assert "[Current Turn]" in framings[1].content
    assert "[Current Turn — New Attachment(s)]" not in framings[1].content


# ── Gating logic — channel-thread context ───────────────────────────


def test_channel_thread_framing_alone_in_short_conversation():
    framings = build_turn_framings(
        history_len=1,
        has_current_attachments=False,
        has_channel_thread_context=True,
    )
    assert len(framings) == 1
    assert "[Channel Thread]" in framings[0].content


def test_channel_thread_suppresses_generic_current_turn_framing():
    """The two reminders would be redundant — keep only the channel-thread one.

    This is the regression test for the inconsistency reviewers flagged
    between ``general_chat.py`` and ``agent_chat.py``.
    """
    framings = build_turn_framings(
        history_len=10,
        has_current_attachments=False,
        has_channel_thread_context=True,
    )
    assert len(framings) == 1
    assert "[Channel Thread]" in framings[0].content
    # Critical: no duplicate / contradictory ``[Current Turn]`` reminder.
    contents = "\n".join(m.content for m in framings)
    assert "[Current Turn]" not in contents


def test_channel_thread_and_attachment_framing_can_stack():
    """A user CAN drop a screenshot into a channel thread — both apply."""
    framings = build_turn_framings(
        history_len=5,
        has_current_attachments=True,
        has_channel_thread_context=True,
    )
    # Channel-thread framing first (sets the scope), then the attachment
    # framing (sets the focus).  No generic ``[Current Turn]`` reminder.
    assert len(framings) == 2
    assert "[Channel Thread]" in framings[0].content
    assert "[Current Turn — New Attachment(s)]" in framings[1].content


# ── Continuation-request matcher ────────────────────────────────────


def test_interrupted_turn_framing_contents():
    msg = interrupted_turn_framing()
    assert msg.role == "system"
    assert "[Continuation Request]" in msg.content
    # The checkpoint demand is the whole reason this framing exists —
    # without it, the LLM either silently restarts from scratch or
    # repeats large chunks of the previous turn.
    assert "checkpoint" in msg.content.lower()
    assert "completion summary" in msg.content.lower()
    # All three checkpoint dimensions must be named explicitly.
    assert "(1)" in msg.content
    assert "(2)" in msg.content
    assert "(3)" in msg.content


def test_is_continue_request_detects_short_variants():
    """Pin every phrase the matcher must recognise — these came from
    real chat traffic where users typed "continue" / "pls continue" /
    "yes go on" / etc. after a turn was cut short.
    """
    assert is_continue_request("continue")
    assert is_continue_request(" Please   Continue ")
    assert is_continue_request("yes, continue")
    assert is_continue_request("can you continue?")
    assert is_continue_request("pls continue")
    assert is_continue_request("go on")
    assert is_continue_request("go ahead")
    assert is_continue_request("resume")
    assert is_continue_request("keep going please")
    # NBSP / zero-width markers from chat-app paste buffers must be
    # stripped before the exact-phrase match runs — otherwise users
    # whose Slack/Notion/Teams autocomplete injected invisible chars
    # would see their explicit "continue" silently ignored.
    assert is_continue_request("please\u00a0continue")
    assert is_continue_request("please\u200b continue")


def test_is_continue_request_ignores_non_continuation_messages():
    """The conservative-by-design contract: scoped instructions that
    HAPPEN to start with ``continue`` (or other phrase tokens) are
    NOT continuation requests.  Misclassifying them would prepend the
    ``interrupted_turn_framing`` checkpoint demand to a fresh-scoped
    request, actively damaging the response shape.
    """
    assert not is_continue_request("continue with the deployment checklist")
    assert not is_continue_request("what did you finish?")
    assert not is_continue_request("")
    assert not is_continue_request("go ahead and create the PR")


# ── Gating logic — continuation framing (centralised in build_turn_framings) ──


def test_continuation_framing_fires_only_when_message_is_continue_request():
    """The centralisation contract: ``build_turn_framings`` injects
    ``interrupted_turn_framing`` ONLY when ``current_user_message`` is
    an explicit continuation request.  This is the regression test for
    duplicated continuation-framing logic in ``agent_chat`` /
    ``general_chat``: once the decision lives here, neither caller can
    drift.
    """
    # Empty history, no attachments — only the continuation framing
    # should fire when the message is a continue request.
    cont = build_turn_framings(
        history_len=1,
        has_current_attachments=False,
        current_user_message="continue",
    )
    assert len(cont) == 1
    assert "[Continuation Request]" in cont[0].content

    # Non-continue message → no continuation framing.
    not_cont = build_turn_framings(
        history_len=1,
        has_current_attachments=False,
        current_user_message="hello, what's up?",
    )
    assert not_cont == []


def test_continuation_framing_can_stack_with_other_framings():
    """The continuation framing MUST stack on top of any other
    framings (channel thread, attachment precedence, generic current-
    turn reminder).  A user can hit "continue" inside a channel thread
    AND after attaching a screenshot, and the LLM benefits from all of
    those signals together.  Order: scope-setters first
    (channel/attachment/generic), then the resume-checkpoint demand
    last so it's the most recent system reminder before the user
    message.
    """
    framings = build_turn_framings(
        history_len=10,
        has_current_attachments=True,
        has_channel_thread_context=True,
        current_user_message="please continue",
    )
    assert len(framings) == 3
    assert "[Channel Thread]" in framings[0].content
    assert "[Current Turn — New Attachment(s)]" in framings[1].content
    assert "[Continuation Request]" in framings[2].content


def test_continuation_framing_omitted_when_current_user_message_is_none():
    """Callers that don't have access to the user message (background
    tasks, future entry points) can omit ``current_user_message`` and
    the continuation framing is silently skipped rather than crashing
    or accidentally firing on a missing message.
    """
    framings = build_turn_framings(
        history_len=5,
        has_current_attachments=False,
        current_user_message=None,
    )
    # Only the generic current-turn reminder fires (history_len > 2).
    assert len(framings) == 1
    assert "[Current Turn]" in framings[0].content
    assert "[Continuation Request]" not in framings[0].content


# ── All framings are role=system ────────────────────────────────────


def test_all_framings_are_system_messages():
    framings = build_turn_framings(
        history_len=10,
        has_current_attachments=True,
        has_channel_thread_context=True,
        current_user_message="continue",
    )
    assert all(m.role == "system" for m in framings)


# ── The current message with the turn's briefs ───────


@pytest.mark.parametrize(
    ("briefs", "expected"),
    [
        pytest.param((), "hello", id="no-brief"),
        pytest.param((None, None), "hello", id="none"),
        pytest.param(("", None), "hello", id="empty"),
        pytest.param(("REFS", None), "hello\n\nREFS", id="references"),
        pytest.param((None, "LINEUP"), "hello\n\nLINEUP", id="line-up"),
        pytest.param(("REFS", "LINEUP"), "hello\n\nREFS\n\nLINEUP", id="both-in-order"),
    ],
)
def test_the_current_message_carries_each_brief_after_the_users_words(briefs, expected):
    assert message_with_briefs("hello", *briefs) == expected


def test_a_brief_on_a_message_without_text_keeps_the_shape_it_always_had():
    assert message_with_briefs("", "REFS") == "\n\nREFS"
