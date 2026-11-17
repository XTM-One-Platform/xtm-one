# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The conversation an agent-account turn sees: pure functions.

The platform thread or chat is the context, not a stored conversation: every
turn reads its last messages from the provider - the human owner's own
messages included - and replays them as a multi-party exchange in which the
account's messages are the assistant's and everyone else's are the user's,
named. Consecutive messages of one side are folded into one turn, because a
provider may refuse two user turns in a row.
"""

from __future__ import annotations

from typing import Any

from app.llm.base import ChatMessage
from app.services.agent_accounts.events import AccountEvent, ContextMessage
from app.services.agent_accounts.rules import Decision

#: The longest text one replayed message keeps.
MESSAGE_TEXT_LIMIT = 2000


def _clip(text: str) -> str:
    """*text* stripped, cut at :data:`MESSAGE_TEXT_LIMIT` with the cut said.

    The marker names what was left out of the replay: without it the model
    read its own earlier reply ending mid-word and reported a message cut in
    the chat, while the posted message was whole.
    """
    text = text.strip()
    if len(text) <= MESSAGE_TEXT_LIMIT:
        return text
    return f"{text[:MESSAGE_TEXT_LIMIT]} [... {len(text) - MESSAGE_TEXT_LIMIT} more characters not replayed]"


_CHANNEL_TYPES = {"direct": "direct_message", "group": "group_chat", "channel": "channel"}


def _line(name: str, text: str) -> str:
    return f"{name.strip() or 'Someone'}: {_clip(text)}"


def absorbed_as_context(absorbed: list[AccountEvent] | tuple[AccountEvent, ...]) -> list[ContextMessage]:
    """The messages a turn absorbed, as lines of its conversation."""
    return [
        ContextMessage(message_id=m.message_id, sender_name=m.sender_name, text=m.text, created_at=m.created_at)
        for m in absorbed
    ]


def build_turn(
    context: list[ContextMessage],
    event: AccountEvent,
    account_name: str | None,
    absorbed: list[AccountEvent] | tuple[AccountEvent, ...] = (),
) -> tuple[list[ChatMessage], str]:
    """``(history, user_message)``: the conversation before *event*, and what to answer.

    The user message holds every message others wrote since the account last
    spoke, *event* last; the account's own messages before anyone else spoke
    are kept as lines of the first user turn, since a conversation cannot open
    on the assistant.

    *absorbed* are the other messages the turn answers (``pipeline._turn_loop``):
    each is in the user message even when the provider's context left
    it out (a context read before it was indexed, or past its limit).
    """
    you = f"{(account_name or 'You').strip()} (you)"
    seen = {m.message_id for m in context if m.message_id}
    extra = [m for m in absorbed_as_context(absorbed) if m.message_id not in seen]
    ordered = sorted(
        (m for m in [*context, *extra] if m.message_id != event.message_id and m.text.strip()),
        key=lambda m: m.created_at,
    )
    history: list[ChatMessage] = []
    pending: list[str] = []
    assistant: list[str] = []
    others_spoke = False

    def flush_user() -> None:
        if pending:
            history.append(ChatMessage(role="user", content="\n".join(pending)))
            pending.clear()

    def flush_assistant() -> None:
        if assistant:
            history.append(ChatMessage(role="assistant", content="\n\n".join(assistant)))
            assistant.clear()

    for message in ordered:
        if message.from_account:
            if not others_spoke:
                pending.append(_line(you, message.text))
                continue
            flush_user()
            assistant.append(_clip(message.text))
        else:
            others_spoke = True
            flush_assistant()
            pending.append(_line(message.sender_name, message.text))
    flush_assistant()
    pending.append(_line(event.sender_name, event.text))
    user_message = "\n".join(pending)
    return history, user_message


def account_brief(
    *,
    mode: str,
    platform_label: str,
    account_name: str | None,
    conversation_name: str | None,
    instructions: str,
    rule_name: str | None = None,
    label: str | None = None,
    merged: int = 0,
    mention_hint: str = "",
) -> str:
    """The few lines that tell the agent whose account it speaks through, and under which rule.

    *label* is the AI label the platform adds to this reply (``Decision.ai_label``),
    ``None`` when it adds none: the people reading then take a delegate's reply as its owner's.
    *mention_hint* is how a reply mentions someone on the platform
    (``AccountProvider.reply_mention_hint``), empty when it cannot.
    """
    name = (account_name or "the account owner").strip()
    labelled = f'the platform adds the line "{label.strip()}" to your reply' if label and label.strip() else ""
    if mode == "ai_user":
        lines = [
            f"You are {name}, an AI colleague with your own {platform_label} account. "
            "The people you talk to know you are an AI. Write as yourself, in the first person, "
            "as a participant of the conversation below" + (f"; {labelled}." if labelled else "."),
        ]
    else:
        lines = [
            f"You are replying in {platform_label} as {name}, on their behalf, in the first person and in "
            "their voice: " + (f"{labelled}." if labelled else "the people you talk to see the message as theirs.")
            + " Never commit them to anything you cannot verify; when unsure, say you will get back to them.",
        ]
    if rule_name and rule_name.strip():
        lines.append(f'You answer under the account\'s rule "{rule_name.strip()}".')
    if conversation_name:
        lines.append(f"Conversation: {conversation_name}.")
    if merged > 1:
        lines.append(merged_note(merged))
    else:
        lines.append(
            "The messages are replayed with their author's name; answer the last one, in the "
            "conversation's language, without restating the conversation."
        )
    if mention_hint:
        lines.append(mention_hint)
    if instructions:
        lines.append(instructions)
    return "\n".join(lines)


def merged_note(count: int) -> str:
    """The line telling the agent its turn answers *count* messages at once."""
    return (
        f"{count} messages merged: they arrived in a row in this conversation and are the last "
        "lines replayed with their author's name. Read them in order and answer them once, in a "
        "single reply, in the conversation's language, without restating the conversation."
    )


def message_context(
    event: AccountEvent,
    decision: Decision,
    *,
    platform: str,
    brief: str,
    triage: bool = False,
    merged_message_ids: list[str] | None = None,
    message_reference: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The ``context`` an agent-account turn runs with (``run_agent_chat(context=...)``).

    With *triage* the turn answers in the triage structure (``triage.output_schema``),
    which carries whether and how to answer: the policy says so.
    *merged_message_ids* are the other messages the turn answers with this one:
    ``coalesced_count`` counts the turn's messages and
    ``coalesced_message_ids`` names them, both recorded on the turn's trace.
    *message_reference* is what the provider's tools take besides ``message_id``
    to act on the message (``AccountProvider.message_reference``: a Teams
    ``chat_id``, or ``team_id`` / ``channel_id`` / ``parent_message_id``), so
    the turn can react to the message it answers.
    """
    context: dict[str, Any] = {
        "platform": platform,
        "channel_type": _CHANNEL_TYPES.get(event.conversation_type, event.conversation_type),
        "is_direct_message": event.conversation_type == "direct",
        "bot_was_mentioned": event.mentions_account,
        "is_thread_continuation": decision.thread_continuation,
        # The rule decides, not the shape of the conversation: a direct message
        # under an "AI decides" rule may stay unanswered, a channel message
        # under an "always" rule may not.
        "response_policy": "schema" if triage else "always" if decision.must_reply else "decide",
        "account_brief": brief,
        # What the account's own tools of the platform take to act on the
        # message answered (read an email's attachments, for instance).
        "message_id": event.message_id,
    }
    for key, value in (message_reference or {}).items():
        if value and key not in context:
            context[key] = value
    if merged_message_ids:
        context["coalesced_count"] = len(merged_message_ids) + 1
        context["coalesced_message_ids"] = [*merged_message_ids, event.message_id]
    if event.sender_name:
        context["contact_name"] = event.sender_name
    if event.sender_email:
        context["sender_email"] = event.sender_email
    if event.conversation_name and event.conversation_type != "direct":
        context["conversation_name"] = event.conversation_name
    return context
