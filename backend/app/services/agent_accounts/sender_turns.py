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

"""One turn at a time per sender of an agent, across its accounts, in Redis.

``turns.py`` holds one turn per conversation of ONE account. A person who
writes to the same agent on two of its accounts (a chat account and a mailbox
account, a message then a forwarded email a minute later) still started two
turns side by side: each read only its own conversation, one of them acted
(sent an email) before the other had seen the sender's correction, and the
other answered by guessing what its sibling did.

This module serializes the turns of one ``(agent, sender)``, whatever account
or conversation brought the message:

- **hold**: a turn takes the sender (:func:`hold`, a Redis lock keyed by the
  agent and the sender's normalized address, refreshed while the turn runs,
  ``LOCK_TTL_SECONDS`` after a dead process);
- **steer**: a message of the same sender that finds a turn in flight on
  another conversation is pushed into that turn's steering inbox, which a
  bridge moves into the agent loop's ``steering_queue`` at its next iteration:
  the running turn reads it before its next tool call (a send included);
- **wait**: that message's own turn then waits for the sender to be free (at
  most ``WAIT_SECONDS``) and runs after it, never beside it; while it waits it
  holds no database connection (``before_wait``);
- **recent**: every inbound message and every answer of the sender's turns is
  logged for ``RECENT_WINDOW_SECONDS`` (:func:`note`), and a turn's user
  message carries what the same sender wrote, and what the agent answered, on
  the agent's other conversations meanwhile (:func:`recent_elsewhere`,
  :func:`with_recent`). A turn cut by a restart and resumed later reads the
  messages that arrived while it was down the same way.

A sender with no address is not serialized. Every call fails open: without
Redis each message is its own turn, as before. Pure of any provider: the
label of the channel is the caller's.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.services.agent_accounts.events import AccountEvent
from app.tools.agent_loop import SteeringMessage, SteeringQueue

logger = logging.getLogger(__name__)

#: How long a sender stays held without a refresh (a process that died mid-turn).
LOCK_TTL_SECONDS = 120
#: How long a message waits for the sender's turn in flight before it runs anyway.
WAIT_SECONDS = 600
#: How often a waiting turn looks again, and the bridge moves the inbox into the loop.
POLL_SECONDS = 1.0
#: How long the sender's messages and the agent's answers stay readable by the next turns.
RECENT_WINDOW_SECONDS = 15 * 60
#: The most entries the sender's log keeps.
RECENT_MAX_ENTRIES = 30
#: The longest text one logged entry keeps.
RECENT_TEXT_LIMIT = 1500
#: How long a steering inbox entry waits for its bridge at most.
INBOX_TTL_SECONDS = 600

_current_queue: contextvars.ContextVar[SteeringQueue | None] = contextvars.ContextVar(
    "agent_account_sender_steering", default=None
)


def sender_key(event: AccountEvent) -> str | None:
    """The sender of *event* across providers: its primary address, lower case; ``None`` without one."""
    # A message that names no address of its sender is not serialized.
    for address in getattr(event, "sender_addresses", None) or ():
        if isinstance(address, str) and "@" in address:
            return address.strip().lower()
    return None


def _key(kind: str, agent_id: str, *parts: str) -> str:
    from app.services.agent_accounts.state import _key as state_key

    return state_key(kind, agent_id, *parts)


def _lock_key(agent_id: str, sender: str) -> str:
    return _key("sender_turn", agent_id, sender)


def _inbox_key(agent_id: str, holder: str) -> str:
    return _key("sender_turn_inbox", agent_id, holder)


def _recent_key(agent_id: str, sender: str) -> str:
    return _key("sender_recent", agent_id, sender)


async def _redis() -> Any | None:
    from app.services.agent_accounts import state

    return await state._redis()


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def _decode(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(_text(raw))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def current_queue() -> SteeringQueue | None:
    """The steering queue of the sender's turn this task runs, or ``None``."""
    return _current_queue.get()


def steer_text(event: AccountEvent, label: str) -> str:
    """The user steer the running turn reads for a message of its sender on another conversation."""
    where = f"{label}, {event.conversation_name}" if event.conversation_name else label
    name = (event.sender_name or "the sender").strip() or "the sender"
    return (
        f"[New message from {name} on {where}, received while you were working on this turn]\n"
        f"{event.text.strip()[:RECENT_TEXT_LIMIT]}\n\n"
        "Take it into account before any further action (a send included). It also reaches you "
        "as its own turn once this one ends, with what this turn did."
    )


async def _offer(
    redis: Any, agent_id: str, current: dict[str, Any], event: AccountEvent, account_id: str, label: str
) -> bool:
    """Push *event* to the inbox of the turn *current* names; whether it was pushed."""
    holder = str(current.get("holder") or "")
    if not holder:
        return False
    if current.get("account") == account_id and current.get("conversation") == event.conversation_id:
        # The same conversation of the same account: its own turns already queue it.
        return False
    inbox = _inbox_key(agent_id, holder)
    await redis.rpush(inbox, json.dumps({"message_id": event.message_id, "text": steer_text(event, label)}))
    await redis.expire(inbox, INBOX_TTL_SECONDS)
    return True


async def _take(
    redis: Any,
    agent_id: str,
    sender: str,
    marker: str,
    event: AccountEvent,
    account_id: str,
    label: str,
    before_wait: Callable[[], Awaitable[Any]] | None,
) -> bool:
    """Take the sender for *marker*, steering *event* into the turn in flight and waiting for it."""
    lock = _lock_key(agent_id, sender)
    if await redis.set(lock, marker, nx=True, ex=LOCK_TTL_SECONDS):
        return True
    offered_to: str | None = None
    deadline = time.monotonic() + WAIT_SECONDS
    if before_wait is not None:
        with contextlib.suppress(Exception):
            await before_wait()
    while True:
        current = _decode(await redis.get(lock))
        holder = str(current.get("holder") or "")
        if holder and holder != offered_to:
            if await _offer(redis, agent_id, current, event, account_id, label):
                logger.info(
                    "Agent account %s steers message %s into the sender's turn in flight", account_id, event.message_id
                )
            offered_to = holder
        if time.monotonic() >= deadline:
            logger.warning(
                "Agent account %s answers message %s beside the sender's turn in flight (waited %ss)",
                account_id,
                event.message_id,
                WAIT_SECONDS,
            )
            return False
        await asyncio.sleep(POLL_SECONDS)
        if await redis.set(lock, marker, nx=True, ex=LOCK_TTL_SECONDS):
            return True


async def _release(redis: Any, agent_id: str, sender: str, marker: str, holder: str) -> None:
    lock = _lock_key(agent_id, sender)
    try:
        if _text(await redis.get(lock)) == marker:
            await redis.delete(lock)
        await redis.delete(_inbox_key(agent_id, holder))
    except Exception:
        logger.debug("Could not release an agent account sender turn", exc_info=True)


async def _pump(redis: Any, agent_id: str, sender: str, marker: str, holder: str, queue: SteeringQueue) -> None:
    """Refresh the hold, and move the steering inbox into the loop's queue."""
    lock = _lock_key(agent_id, sender)
    if _text(await redis.get(lock)) == marker:
        await redis.expire(lock, LOCK_TTL_SECONDS)
    inbox = _inbox_key(agent_id, holder)
    while True:
        raw = await redis.lpop(inbox)
        if raw is None:
            return
        entry = _decode(raw)
        if entry.get("text"):
            queue.put_nowait(SteeringMessage(text=str(entry["text"])))


@contextlib.asynccontextmanager
async def hold(
    agent_id: object,
    account_id: str,
    event: AccountEvent,
    *,
    label: str,
    before_wait: Callable[[], Awaitable[Any]] | None = None,
) -> AsyncIterator[SteeringQueue | None]:
    """Run the body as the sender's one turn on the agent; yields its steering queue.

    ``None`` when the sender has no address, the agent is unknown, Redis is
    unreachable, or the turn in flight outlasted ``WAIT_SECONDS``: the body
    runs as before, unserialized. Never raises on its own account.
    """
    sender = sender_key(event)
    redis = await _redis() if sender and agent_id else None
    if redis is None or sender is None:
        yield None
        return
    agent = str(agent_id)
    holder = secrets.token_hex(8)
    marker = json.dumps({"holder": holder, "account": account_id, "conversation": event.conversation_id})
    try:
        taken = await _take(redis, agent, sender, marker, event, account_id, label, before_wait)
    except Exception:
        logger.debug("Agent account sender turn failed open", exc_info=True)
        taken = False
    if not taken:
        yield None
        return
    queue = SteeringQueue()

    async def _bridge() -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await _pump(redis, agent, sender, marker, holder, queue)
            except Exception:
                logger.debug("Agent account sender turn: inbox poll failed (retrying)", exc_info=True)

    with contextlib.suppress(Exception):
        await _pump(redis, agent, sender, marker, holder, queue)
    bridge = asyncio.create_task(_bridge())
    token = _current_queue.set(queue)
    try:
        yield queue
    finally:
        _current_queue.reset(token)
        bridge.cancel()
        with contextlib.suppress(BaseException):
            await bridge
        await _release(redis, agent, sender, marker, holder)


async def note(
    agent_id: object,
    account_id: str,
    event: AccountEvent,
    *,
    label: str,
    direction: str = "in",
    text: str | None = None,
    status: str | None = None,
    now: datetime | None = None,
) -> None:
    """Log a message of the sender (``in``) or the agent's answer to it (``out``). Never raises."""
    sender = sender_key(event)
    if not sender or not agent_id:
        return
    redis = await _redis()
    if redis is None:
        return
    body = (event.text if text is None else text) or ""
    entry = {
        "account": account_id,
        "conversation": event.conversation_id,
        "conversation_name": event.conversation_name or "",
        "message_id": event.message_id,
        "label": label,
        "direction": direction,
        "status": status or "",
        "at": (now or datetime.now(UTC)).isoformat(),
        "text": body.strip()[:RECENT_TEXT_LIMIT],
    }
    key = _recent_key(str(agent_id), sender)
    try:
        await redis.rpush(key, json.dumps(entry, sort_keys=True))
        await redis.ltrim(key, -RECENT_MAX_ENTRIES, -1)
        await redis.expire(key, RECENT_WINDOW_SECONDS)
    except Exception:
        logger.debug("Could not log an agent account sender message", exc_info=True)


async def recent_elsewhere(
    agent_id: object, account_id: str, event: AccountEvent, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The sender's log on the agent's OTHER conversations within the window, oldest first."""
    sender = sender_key(event)
    if not sender or not agent_id:
        return []
    redis = await _redis()
    if redis is None:
        return []
    try:
        raw = await redis.lrange(_recent_key(str(agent_id), sender), 0, -1)
    except Exception:
        return []
    cutoff = (now or datetime.now(UTC)).timestamp() - RECENT_WINDOW_SECONDS
    out: list[dict[str, Any]] = []
    for item in raw or []:
        entry = _decode(item)
        try:
            at = datetime.fromisoformat(str(entry.get("at"))).timestamp()
        except ValueError:
            continue
        if at < cutoff:
            continue
        if entry.get("account") == account_id and entry.get("conversation") == event.conversation_id:
            continue
        if entry.get("message_id") == event.message_id and entry.get("direction") == "in":
            continue
        out.append(entry)
    out.sort(key=lambda e: str(e.get("at")))
    return out


def with_recent(user_message: str, entries: list[dict[str, Any]], sender_name: str | None) -> str:
    """*user_message* followed by the sender's recent messages and the agent's answers elsewhere."""
    if not entries:
        return user_message
    name = (sender_name or "The sender").strip() or "The sender"
    lines = []
    for entry in entries:
        where = entry.get("label") or "another channel"
        if entry.get("conversation_name"):
            where = f"{where}, {entry['conversation_name']}"
        at = str(entry.get("at") or "")[:16].replace("T", " ")
        if entry.get("direction") == "out":
            status = f" ({entry['status']})" if entry.get("status") else ""
            lines.append(f"- [{at}Z, {where}] you answered{status}: {entry.get('text') or ''}")
        else:
            lines.append(f"- [{at}Z, {where}] {name}: {entry.get('text') or ''}")
    return (
        f"{user_message}\n\n"
        f"[Context: what {name} wrote to you, and what you answered, on your other channels in the last "
        f"{RECENT_WINDOW_SECONDS // 60} minutes. The same person may be continuing one request there: do not act "
        "twice, and follow the latest instruction.]\n" + "\n".join(lines)
    )
