"""The conversation an agent-account turn sees: the platform thread, replayed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.agent_accounts.conversation import account_brief, build_turn, message_context
from app.services.agent_accounts.events import AccountEvent, ContextMessage
from app.services.agent_accounts.rules import Decision
from app.services.message_context import render_message_context

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def _msg(i: int, name: str, text: str, *, mine: bool = False) -> ContextMessage:
    return ContextMessage(
        message_id=f"m{i}", sender_name=name, text=text, created_at=T0 + timedelta(minutes=i), from_account=mine
    )


def _event(text: str = "And the numbers?", message_id: str = "m9") -> AccountEvent:
    return AccountEvent(
        provider="test",
        conversation_type="group",
        conversation_id="chat-1",
        thread_id="chat-1",
        message_id=message_id,
        sender_id="bob-id",
        sender_name="Bob",
        text=text,
        created_at=T0 + timedelta(minutes=9),
        conversation_name="Q4 planning",
    )


def test_the_thread_is_replayed_as_alternating_turns_with_the_authors_named():
    context = [
        _msg(1, "Alice", "Hi Sam"),
        _msg(2, "Bob", "Are we on for Friday?"),
        _msg(3, "Sam", "Yes, Friday works.", mine=True),
        _msg(4, "Sam", "I will bring the deck.", mine=True),
        _msg(5, "Alice", "Great"),
    ]
    history, user_message = build_turn(context, _event(), "Sam")
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "Alice: Hi Sam\nBob: Are we on for Friday?"
    assert history[1].content == "Yes, Friday works.\n\nI will bring the deck."
    # Everything others wrote since the account last spoke, the message to answer last.
    assert user_message == "Alice: Great\nBob: And the numbers?"


def test_a_conversation_never_opens_on_the_assistant():
    context = [_msg(1, "Sam", "Kick-off notes are in the channel.", mine=True), _msg(2, "Alice", "Thanks")]
    history, user_message = build_turn(context, _event(), "Sam")
    assert history == []
    assert user_message.splitlines() == [
        "Sam (you): Kick-off notes are in the channel.",
        "Alice: Thanks",
        "Bob: And the numbers?",
    ]


def test_the_answered_message_is_not_replayed_twice_and_order_is_by_time():
    context = [_msg(9, "Bob", "And the numbers?"), _msg(2, "Alice", "second"), _msg(1, "Alice", "first")]
    history, user_message = build_turn(context, _event(), "Sam")
    assert history == []
    assert user_message == "Alice: first\nAlice: second\nBob: And the numbers?"


def test_a_message_the_turn_absorbed_is_in_the_prompt_even_when_the_context_left_it_out():
    """Two messages 5 s apart from two senders, one turn: both texts reach the model."""
    stephane = AccountEvent(
        provider="test",
        conversation_type="group",
        conversation_id="chat-1",
        thread_id="chat-1",
        message_id="s1",
        sender_id="stephane-id",
        sender_name="Stephane",
        text="Could we use xtm-docs for the docs?",
        created_at=T0 + timedelta(minutes=9) - timedelta(seconds=5),
    )
    jp = _event(text="https://github.com/XTM-Flow/xtm-docs")
    # The provider's context was read before Stephane's message was indexed.
    history, user_message = build_turn([_msg(1, "Alice", "Hi")], jp, "Sam", [stephane])
    assert history == []
    assert user_message == (
        "Alice: Hi\nStephane: Could we use xtm-docs for the docs?\nBob: https://github.com/XTM-Flow/xtm-docs"
    )
    # Present in the context too: replayed once.
    context = [_msg(1, "Alice", "Hi"), ContextMessage("s1", "Stephane", stephane.text, stephane.created_at)]
    _history, again = build_turn(context, jp, "Sam", [stephane])
    assert again == user_message


def test_a_merged_turn_says_how_many_messages_it_answers():
    brief = account_brief(
        mode="on_behalf", platform_label="Microsoft Teams", account_name="Sam", conversation_name=None,
        instructions="", merged=2,
    )
    assert "2 messages merged" in brief and "answer the last one" not in brief
    single = account_brief(
        mode="on_behalf", platform_label="Microsoft Teams", account_name="Sam", conversation_name=None,
        instructions="",
    )
    assert "messages merged" not in single and "answer the last one" in single
    context = message_context(
        _event(), _decision(), platform="teams", brief=brief, merged_message_ids=["s1"]
    )
    assert context["coalesced_count"] == 2 and context["coalesced_message_ids"] == ["s1", "m9"]
    section, _quarantined = render_message_context(context)
    assert "coalesced_count: 2" in section and "coalesced_message_ids" not in section
    assert "coalesced_count" not in message_context(_event(), _decision(), platform="teams", brief="B")


def test_a_long_message_is_cut():
    history, user_message = build_turn([], _event(text="x" * 5000), "Sam")
    assert history == [] and user_message.endswith("[... 3000 more characters not replayed]")
    assert len(user_message) < 2100


def test_the_accounts_own_long_reply_is_replayed_with_its_cut_said() -> None:
    """The account's own reply past the limit never reads as a message cut mid-word in the chat."""
    reply = "It's a backend change. " * 120  # 2,760 characters, posted whole
    context = [_msg(1, "Alice", "Status?"), _msg(2, "Sam", reply, mine=True), _msg(3, "Alice", "Thanks")]
    history, _user_message = build_turn(context, _event(), "Sam")
    assert [m.role for m in history] == ["user", "assistant"]
    replayed = history[1].content
    assert replayed.startswith(reply.strip()[:2000])
    assert replayed.endswith(f"[... {len(reply.strip()) - 2000} more characters not replayed]")


def test_a_short_reply_is_replayed_unchanged() -> None:
    context = [_msg(1, "Alice", "Status?"), _msg(2, "Sam", "  Done.  ", mine=True), _msg(3, "Alice", "Thanks")]
    history, _user_message = build_turn(context, _event(), "Sam")
    assert history[1].content == "Done."


def _decision(**fields) -> Decision:
    return Decision(True, "respond", **fields)


def test_the_message_context_states_the_rules_policy_not_the_conversations_shape():
    always = message_context(_event(), _decision(must_reply=True), platform="teams", brief="B")
    assert always["response_policy"] == "always" and always["account_brief"] == "B"
    assert always["channel_type"] == "group_chat" and always["is_direct_message"] is False
    decide = message_context(_event(), _decision(thread_continuation=True), platform="teams", brief="B")
    assert decide["response_policy"] == "decide" and decide["is_thread_continuation"] is True


def test_the_brief_and_policy_never_render_as_member_typed_metadata():
    context = message_context(_event(), _decision(must_reply=True), platform="teams", brief="You are Sam.")
    section, _quarantined = render_message_context(context)
    assert "You are Sam." not in section and "response_policy" not in section
    assert "Bob" in section  # the sender's name is still there, fenced


def test_the_message_reference_reaches_the_turn_as_structural_ids():
    """A Teams turn can react to the message it answers: the ids its tools take are in the context."""
    context = message_context(
        _event(),
        _decision(),
        platform="teams",
        brief="B",
        message_reference={"chat_id": "19:g@thread.v2", "message_id": "other", "parent_message_id": ""},
    )
    assert context["chat_id"] == "19:g@thread.v2" and context["message_id"] == "m9"
    assert "parent_message_id" not in context
    section, _quarantined = render_message_context(context)
    head = section.split("<<", 1)[0]
    assert "chat_id: 19:g@thread.v2" in head and "message_id: m9" in head
    assert "chat_id" not in message_context(_event(), _decision(), platform="teams", brief="B")


def test_the_brief_says_whose_voice_to_use():
    delegate = account_brief(
        mode="on_behalf", platform_label="Microsoft Teams", account_name="Sam", conversation_name=None,
        instructions="Keep it short.",
    )
    assert "as Sam, on their behalf" in delegate and delegate.endswith("Keep it short.")
    ai_user = account_brief(
        mode="ai_user", platform_label="Microsoft Teams", account_name="Ada", conversation_name="#incidents",
        instructions="",
    )
    assert "You are Ada, an AI colleague" in ai_user and "Conversation: #incidents." in ai_user
