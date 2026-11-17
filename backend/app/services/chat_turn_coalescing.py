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

"""One turn per conversation for chat-channel triggers.

A chat channel (a Teams 1:1 or group chat, a Teams channel thread) delivers
one trigger per inbound message. Two messages a few seconds apart used to
start two independent runs, which both did the work and both replied: the
sender read the same answer twice. This module holds the pure
rules the assignment engine applies to such a *conversation event*:

A conversation event is an integration event whose payload carries a
``conversation_key`` (``assignment_dispatch.CONVERSATION_KEY_FIELD``), set by
the channel's own payload builder (``event_poller.teams_chat_payload``,
``teams_reply_payload`` and ``slack_conversations.conversation_key`` today):
this module never names a channel.

- **ordering**: the event's default ordering key is that conversation
  (``assignment_dispatch.resolve_ordering_key``), so at most one run per
  (assignment, conversation) is in flight and a message arriving mid-run waits
  in the delivery queue behind it, whatever ``INTEGRATION_EVENT_QUEUE_ENABLED``
  says (``forces_queue``);
- **coalescing window**: a conversation event's run starts
  ``window_seconds`` late (``CHAT_EVENT_COALESCE_WINDOW_SECONDS``, default 6,
  ``trigger_config.coalesce_window_seconds`` overrides, ``0`` turns it off);
- **merge**: a later message of the same conversation and the same sender that
  finds that run still waiting (delayed in its window, or queued) is merged
  into it (``merged_payload``): one turn, the messages in order, the text
  joined, ``coalesced_count`` and a note telling the agent to answer once.

Everything here is pure and unit-tested; the SQL stays in the engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.assignment_dispatch import CONVERSATION_KEY_FIELD

#: ``trigger_config`` key of an assignment's own coalescing window.
WINDOW_CONFIG = "coalesce_window_seconds"
#: ``trigger_context`` key recording the window a run was given.
WINDOW_CONTEXT = "coalesce_window_seconds"
MAX_WINDOW_SECONDS = 60
#: A run whose window ends sooner than this is left alone: it may be claimed
#: by the reconciler while the merge commits, and read its payload before it.
MERGE_MARGIN_SECONDS = 1.0
#: Messages one turn carries at most; the next one starts a turn of its own.
MAX_MERGED_MESSAGES = 20

#: Payload keys kept per message in ``messages``.
_MESSAGE_KEYS = ("message_id", "text", "from_name", "from_id", "created_at")


def is_conversation_event(trigger_context: dict | None, payload: dict | None) -> bool:
    """Whether a delivery is a message of a chat conversation."""
    return (
        isinstance(trigger_context, dict)
        and trigger_context.get("type") == "integration_event"
        and isinstance(payload, dict)
        and bool(payload.get(CONVERSATION_KEY_FIELD))
    )


def forces_queue(trigger_context: dict | None, payload: dict | None) -> bool:
    """A busy conversation event waits in the queue rather than being dropped.

    Dropping it would lose a message the sender expects an answer to; running it
    beside the run of its conversation would answer twice.
    """
    return is_conversation_event(trigger_context, payload)


def window_seconds(trigger_config: dict | None, default: int) -> int:
    """The coalescing window of an assignment: its own, else *default*; 0..60."""
    raw = trigger_config.get(WINDOW_CONFIG) if isinstance(trigger_config, dict) else None
    value: Any = default if raw is None or isinstance(raw, bool) else raw
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = int(default)
    return max(0, min(MAX_WINDOW_SECONDS, seconds))


def sender_of(payload: dict | None) -> str:
    """The sender of a conversation message: its id when the payload has one, else its name."""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("from_id") or payload.get("from_name") or "").strip()


def messages_of(payload: dict | None) -> list[dict]:
    """The messages a run's payload carries, oldest first (one when never merged)."""
    if not isinstance(payload, dict):
        return []
    merged = payload.get("messages")
    if isinstance(merged, list) and merged:
        return [m for m in merged if isinstance(m, dict)]
    return [{k: payload[k] for k in _MESSAGE_KEYS if payload.get(k) not in (None, "")}]


def carries(waiting: dict | None, incoming: dict | None) -> bool:
    """Whether the waiting turn already carries *incoming* (the same message delivered twice)."""
    message_id = (incoming or {}).get("message_id")
    return bool(message_id) and any(m.get("message_id") == message_id for m in messages_of(waiting))


def can_merge(waiting: dict | None, incoming: dict | None) -> bool:
    """Whether *incoming* joins the turn of the waiting run's payload *waiting*.

    Same sender, a sender known on both sides, room left in the turn, and not a
    message the turn already carries (``carries``).
    """
    sender = sender_of(incoming)
    if not sender or sender != sender_of(waiting):
        return False
    if len(messages_of(waiting)) >= MAX_MERGED_MESSAGES:
        return False
    return not carries(waiting, incoming)


def still_open(run_at: datetime | None, now: datetime) -> bool:
    """Whether a delayed run's window leaves time to merge into it safely."""
    return run_at is not None and run_at - now > timedelta(seconds=MERGE_MARGIN_SECONDS)


def coalesced_note(count: int) -> str:
    return (
        f"{count} messages merged: the sender wrote them in a row in this conversation. "
        "Read them in order and answer them once, in a single reply."
    )


def merged_payload(waiting: dict, incoming: dict) -> dict:
    """The waiting run's payload with *incoming* merged in, as one turn.

    The first message's fields stay where they were (``message_id`` names the
    turn's first message, so the run's dedup key is unchanged); ``messages``
    lists every message in order, ``message_ids`` their ids, ``text`` joins
    their texts, ``coalesced_count`` counts them and ``coalesced_note`` tells
    the agent to answer once.
    """
    messages = [*messages_of(waiting), *messages_of(incoming)]
    merged = dict(waiting)
    merged["messages"] = messages
    merged["message_ids"] = [m["message_id"] for m in messages if m.get("message_id")]
    merged["text"] = "\n\n".join(str(m.get("text") or "") for m in messages if m.get("text"))
    merged["coalesced_count"] = len(messages)
    merged["coalesced_note"] = coalesced_note(len(messages))
    return merged
