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

"""The conversation a Slack message belongs to.

A ``new_slack_message`` delivery carries the ``conversation_key`` of its
conversation (``assignment_dispatch.CONVERSATION_KEY_FIELD``), so the shared
chat turn model applies to Slack as it does to Teams: one run per
(assignment, conversation), a sender's rapid messages coalesced into one
turn, a message arriving mid-run steered into it, and one reply per inbound
message at the send (``chat_reply_guard``).

- a reply in a thread (``thread_ts`` set and not the message's own ``ts``)
  belongs to that thread: ``slack-thread:<channel>:<thread_ts>``;
- any other message (a DM, a top-level channel message) belongs to its
  channel or DM: ``slack-channel:<channel>``.

A send claims the turn's inbound ids when it lands in the conversation the
run answers: a post in the channel, or a reply in a thread of it (the agent
answering a top-level message in its thread), or a reply in the thread the
turn came from (``reply_keys``).
"""

from __future__ import annotations

from app.services import chat_reply_guard

#: The channel name of Slack sends in ``chat_reply_guard`` (its refusal metric label).
REPLY_GUARD_CHANNEL = "slack"


def channel_conversation_key(channel_id: object) -> str:
    """The ``conversation_key`` of a Slack DM or channel (its top-level messages)."""
    channel = str(channel_id or "").strip()
    return f"slack-channel:{channel}" if channel else ""


def thread_conversation_key(channel_id: object, thread_ts: object) -> str:
    """The ``conversation_key`` of a Slack thread, by its root message ``ts``."""
    channel = str(channel_id or "").strip()
    thread = str(thread_ts or "").strip()
    return f"slack-thread:{channel}:{thread}" if channel and thread else ""


def conversation_key(channel_id: object, ts: object, thread_ts: object = None) -> str:
    """The ``conversation_key`` of an inbound Slack message."""
    thread = str(thread_ts or "").strip()
    if thread and thread != str(ts or "").strip():
        return thread_conversation_key(channel_id, thread)
    return channel_conversation_key(channel_id)


def reply_keys(channel_id: object, thread_ts: object = None) -> list[str]:
    """The conversations a send in *channel_id* (in thread *thread_ts* when given) answers."""
    keys = [thread_conversation_key(channel_id, thread_ts)] if thread_ts else []
    keys.append(channel_conversation_key(channel_id))
    return [k for k in keys if k]


async def claim_reply(channel_id: object, thread_ts: object = None) -> chat_reply_guard.Claim:
    """Claim the executing turn's inbound ids before a Slack post (``chat_reply_guard.claim``)."""
    turn = chat_reply_guard.current_turn()
    keys = reply_keys(channel_id, thread_ts)
    key = turn.conversation_key if turn is not None and turn.conversation_key in keys else ""
    return await chat_reply_guard.claim(REPLY_GUARD_CHANNEL, key)
