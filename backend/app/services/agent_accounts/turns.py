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

"""One turn at a time per conversation of a chat agent account, in Redis.

A chat platform notifies every message on its own, so two messages a few
seconds apart in one chat (a WhatsApp chat or group, a Teams or Slack chat)
reached two independent turns of the agent: both read the conversation, both
answered, and the person saw the same answer twice. The pipeline now holds
one turn per ``(account, conversation)``:

- the message that finds no turn in flight takes the conversation
  (:func:`take`) and is answered after a short coalescing window
  (``settings.chat_event_coalesce_window_seconds``), so the messages a sender
  sends in a burst are answered once;
- a message that finds a turn in flight is queued behind it (:func:`enqueue`)
  and never answered on its own;
- the holder answers, then drains the queue (:func:`drain`): the queued
  messages become ONE next turn, answered on the latest of them. The turn
  replays the platform conversation (``conversation.build_turn``: every
  message others wrote since the account last spoke), so that turn sees every
  queued message and the reply the previous turn posted;
- a process that died holding a conversation leaves its lock to expire
  (``TURN_TTL_SECONDS``) and its queue indexed (``QUEUES_KEY``): the
  maintenance pass hands a queue whose conversation nobody holds any more to
  a new turn (:func:`orphaned`), so a queued message is not lost on a restart.

The release closes the race with a message queued at the same instant: an
arrival that could not take the conversation queues then tries to take it
again, and the holder that released it looks at the queue again, so a queued
message always has a turn coming. Every call fails open: without Redis each
message is its own turn, as before.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from app.services.agent_accounts.events import AccountEvent

logger = logging.getLogger(__name__)

#: How long a conversation stays held without its release (a dead process).
TURN_TTL_SECONDS = 10 * 60
#: How long queued messages wait for a turn before they are dropped.
QUEUE_TTL_SECONDS = 60 * 60
#: The most messages one queue keeps: a flood is answered on its latest ones.
QUEUE_MAX_MESSAGES = 20
#: The sorted set of conversations with queued messages, scored by last enqueue time.
QUEUES_KEY = "agent_account:turn_queues"
#: A queue this quiet whose conversation nobody holds is one a dead process left.
ORPHAN_AFTER_SECONDS = 60


def _conversation(account_id: str, conversation_id: str) -> str:
    return json.dumps([account_id, conversation_id])


def _lock_key(account_id: str, conversation_id: str) -> str:
    from app.services.agent_accounts.state import _key

    return _key("turn", account_id, conversation_id)


def _queue_key(account_id: str, conversation_id: str) -> str:
    from app.services.agent_accounts.state import _key

    return _key("turn_queue", account_id, conversation_id)


async def _redis() -> Any | None:
    from app.services.agent_accounts import state

    return await state._redis()


def new_holder() -> str:
    """A token naming one turn, so a turn only ever releases its own hold."""
    return secrets.token_hex(8)


async def take(account_id: str, conversation_id: str, holder: str) -> bool | None:
    """Hold the conversation for *holder*: ``True`` taken, ``False`` a turn is in flight.

    ``None`` when Redis is unavailable: the message is answered on its own,
    without a window, as before the turn lock.
    """
    redis = await _redis()
    if redis is None:
        return None
    try:
        return bool(await redis.set(_lock_key(account_id, conversation_id), holder, nx=True, ex=TURN_TTL_SECONDS))
    except Exception:
        logger.debug("Agent account turn lock failed open", exc_info=True)
        return None


async def release(account_id: str, conversation_id: str, holder: str) -> None:
    """Give the conversation back, only if *holder* still holds it."""
    redis = await _redis()
    if redis is None:
        return
    key = _lock_key(account_id, conversation_id)
    try:
        current = await redis.get(key)
        if isinstance(current, bytes):
            current = current.decode()
        if current == holder:
            await redis.delete(key)
    except Exception:
        logger.debug("Could not release an agent account turn", exc_info=True)


async def enqueue(account_id: str, conversation_id: str, event: AccountEvent) -> bool:
    """Queue *event* behind the conversation's turn; whether it was queued."""
    redis = await _redis()
    if redis is None:
        return False
    key = _queue_key(account_id, conversation_id)
    try:
        await redis.rpush(key, json.dumps(event.to_dict(), sort_keys=True))
        await redis.ltrim(key, -QUEUE_MAX_MESSAGES, -1)
        await redis.expire(key, QUEUE_TTL_SECONDS)
        await redis.zadd(QUEUES_KEY, {_conversation(account_id, conversation_id): time.time()})
        return True
    except Exception:
        logger.warning("Could not queue an agent account message behind its conversation's turn")
        return False


async def drain(account_id: str, conversation_id: str) -> list[AccountEvent]:
    """The queued messages of the conversation, in arrival order, taken off the queue."""
    redis = await _redis()
    if redis is None:
        return []
    key = _queue_key(account_id, conversation_id)
    events: list[AccountEvent] = []
    try:
        for _ in range(QUEUE_MAX_MESSAGES):
            raw = await redis.lpop(key)
            if raw is None:
                break
            try:
                events.append(AccountEvent.from_dict(json.loads(raw)))
            except Exception:
                logger.warning("Dropped an unreadable queued agent account message")
        if not await redis.exists(key):
            await redis.zrem(QUEUES_KEY, _conversation(account_id, conversation_id))
    except Exception:
        logger.debug("Could not drain an agent account turn queue", exc_info=True)
    return events


async def has_queued(account_id: str, conversation_id: str) -> bool:
    redis = await _redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_queue_key(account_id, conversation_id)))
    except Exception:
        return False


async def orphaned(limit: int = 50) -> list[tuple[str, str]]:
    """Conversations with queued messages that no turn holds any more (a dead process)."""
    redis = await _redis()
    if redis is None:
        return []
    cutoff = time.time() - ORPHAN_AFTER_SECONDS
    found: list[tuple[str, str]] = []
    try:
        members = await redis.zrangebyscore(QUEUES_KEY, 0, cutoff, start=0, num=limit)
    except Exception:
        return []
    for raw in members or []:
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            account_id, conversation_id = json.loads(member)
            if await redis.exists(_lock_key(account_id, conversation_id)):
                continue
            if not await redis.exists(_queue_key(account_id, conversation_id)):
                await redis.zrem(QUEUES_KEY, member)
                continue
            found.append((str(account_id), str(conversation_id)))
        except Exception:
            logger.warning("Dropped an unreadable agent account turn queue entry")
            await redis.zrem(QUEUES_KEY, member)
    return found
