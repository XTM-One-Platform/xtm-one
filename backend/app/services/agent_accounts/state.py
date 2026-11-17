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

"""What the agent-account pipeline remembers between messages, in Redis.

Short-lived conversation state the rule engine reads through
:class:`~.rules.EventState` - a message seen once, a thread the agent answered
in, the human owner writing in a conversation, the replies of the last hour -
plus the messages waiting out a grace period and the threads followed for
their replies. The decisions themselves are durable (``activity.py``).
Every read fails open (a missing Redis never silences an account
that should answer, nor answers twice what a dedup would have dropped: the
dedup alone fails open towards processing, as the channel adapters' do).
The account-wide turn limit alone fails closed (``take_turn``): anyone
who can write to the account would otherwise set what it spends.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from app.services.agent_accounts.events import AccountEvent

logger = logging.getLogger(__name__)

SEEN_TTL_SECONDS = 24 * 60 * 60
SENT_TTL_SECONDS = 24 * 60 * 60
#: How long a post may take before its echo is no longer waited for.
SENDING_TTL_SECONDS = 30
#: How long a thread the agent answered in keeps counting follow-ups as addressed.
THREAD_ACTIVE_TTL_SECONDS = 24 * 60 * 60
REPLY_WINDOW_SECONDS = 60 * 60
#: The sorted set of messages waiting out a grace period, scored by due time.
PENDING_KEY = "agent_account:pending"
#: A parked message this late is one whose process died: the sweep runs it.
PENDING_STALE_SECONDS = 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _key(kind: str, account_id: str, *parts: str) -> str:
    suffix = ":".join(_digest(p) for p in parts)
    return f"agent_account:{kind}:{account_id}" + (f":{suffix}" if suffix else "")


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception:
        logger.debug("Redis unavailable for agent accounts", exc_info=True)
        return None


async def first_delivery(account_id: str, conversation_id: str, message_id: str) -> bool:
    """Whether this is the first time the account sees the message (notifications repeat).

    A message is named by its conversation AND its id: a platform may number
    messages per conversation (Teams chat message ids are millisecond
    timestamps), so an id alone can name two messages.
    """
    redis = await _redis()
    if redis is None:
        return True
    try:
        key = _key("seen", account_id, conversation_id, message_id)
        return bool(await redis.set(key, "1", nx=True, ex=SEEN_TTL_SECONDS))
    except Exception:
        logger.debug("Agent account dedup failed open", exc_info=True)
        return True


async def mark_sent(account_id: str, conversation_id: str, message_id: str) -> None:
    """Remember a message the agent posted, so its echo is not read as the owner writing."""
    redis = await _redis()
    if redis is None or not message_id:
        return
    try:
        await redis.set(_key("sent", account_id, conversation_id, message_id), "1", ex=SENT_TTL_SECONDS)
    except Exception:
        logger.debug("Could not record an agent account post", exc_info=True)


async def mark_sending(account_id: str, conversation_id: str, sending: bool) -> None:
    """Open or close the window in which the agent's post exists but its id is not known yet."""
    redis = await _redis()
    if redis is None:
        return
    key = _key("sending", account_id, conversation_id)
    try:
        if sending:
            await redis.set(key, "1", ex=SENDING_TTL_SECONDS)
        else:
            await redis.delete(key)
    except Exception:
        logger.debug("Could not record an agent account send in flight", exc_info=True)


async def is_sending(account_id: str, conversation_id: str) -> bool:
    redis = await _redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_key("sending", account_id, conversation_id)))
    except Exception:
        return False


async def was_sent_by_agent(account_id: str, conversation_id: str, message_id: str) -> bool:
    redis = await _redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_key("sent", account_id, conversation_id, message_id)))
    except Exception:
        return False


async def mark_owner_active(account_id: str, conversation_id: str, minutes: int) -> None:
    """The human wrote in *conversation_id*: the agent backs off there for *minutes*."""
    redis = await _redis()
    if redis is None or minutes <= 0:
        return
    try:
        await redis.set(_key("owner", account_id, conversation_id), "1", ex=minutes * 60)
    except Exception:
        logger.debug("Could not record the owner taking over", exc_info=True)


async def mark_replied(account_id: str, *, conversation_id: str, thread_id: str, message_id: str) -> None:
    """The agent answered in *thread_id*: follow-ups continue it, the hour counts it."""
    redis = await _redis()
    if redis is None:
        return
    now = time.time()
    replies = _key("replies", account_id, conversation_id)
    try:
        pipe = redis.pipeline()
        pipe.set(_key("thread", account_id, thread_id), "1", ex=THREAD_ACTIVE_TTL_SECONDS)
        pipe.zadd(replies, {f"{now}:{message_id}": now})
        pipe.zremrangebyscore(replies, 0, now - REPLY_WINDOW_SECONDS)
        pipe.expire(replies, REPLY_WINDOW_SECONDS)
        await pipe.execute()
    except Exception:
        logger.debug("Could not record an agent account reply", exc_info=True)


async def mark_thread_active(account_id: str, thread_id: str) -> None:
    """The account wrote in *thread_id* outside its rules (a tool send): follow-ups continue it.

    Unlike :func:`mark_replied`, the send does not count against the rules'
    replies per hour: no rule produced it.
    """
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.set(_key("thread", account_id, thread_id), "1", ex=THREAD_ACTIVE_TTL_SECONDS)
    except Exception:
        logger.debug("Could not record an agent account thread as active", exc_info=True)


async def conversation_state(account_id: str, event: AccountEvent) -> tuple[bool, bool, int]:
    """``(thread_active, owner_active, replies_last_hour)`` for *event*'s conversation."""
    redis = await _redis()
    if redis is None:
        return False, False, 0
    now = time.time()
    replies = _key("replies", account_id, event.conversation_id)
    try:
        pipe = redis.pipeline()
        pipe.exists(_key("thread", account_id, event.thread_id))
        pipe.exists(_key("owner", account_id, event.conversation_id))
        pipe.zcount(replies, now - REPLY_WINDOW_SECONDS, "+inf")
        thread_active, owner_active, count = await pipe.execute()
        return bool(thread_active), bool(owner_active), int(count or 0)
    except Exception:
        logger.debug("Agent account state read failed open", exc_info=True)
        return False, False, 0


async def account_turns(account_id: str) -> int:
    """The agent turns the account ran in the last hour, every conversation together.

    Read by the rule engine; fails open (0) - :func:`take_turn` is the bound.
    """
    redis = await _redis()
    if redis is None:
        return 0
    try:
        return int(await redis.zcount(_key("turns", account_id), time.time() - REPLY_WINDOW_SECONDS, "+inf") or 0)
    except Exception:
        logger.debug("Agent account turn count read failed open", exc_info=True)
        return 0


async def take_turn(account_id: str, limit: int) -> bool:
    """Count one agent turn of the account in the hour, unless *limit* turns already ran.

    *limit* 0 is no limit. Whoever writes to the account sets how many turns it
    runs, so this one fails closed: without Redis, a limited account runs none.
    """
    if limit <= 0:
        return True
    redis = await _redis()
    if redis is None:
        logger.warning("Agent account %s ran no turn: its hourly limit could not be checked", account_id)
        return False
    now = time.time()
    key = _key("turns", account_id)
    member = f"{now}:{uuid.uuid4().hex}"
    try:
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - REPLY_WINDOW_SECONDS)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, REPLY_WINDOW_SECONDS)
        _removed, _added, count, _expiry = await pipe.execute()
        if int(count or 0) <= limit:
            return True
        await redis.zrem(key, member)
        return False
    except Exception:
        logger.warning("Agent account %s ran no turn: its hourly limit could not be checked", account_id)
        return False


def _pending_member(account_id: str, event: AccountEvent) -> str:
    return json.dumps({"account_id": account_id, "event": event.to_dict()}, sort_keys=True)


async def park(account_id: str, event: AccountEvent, due: float) -> str | None:
    """Keep *event* until *due* (a grace period); the member to claim it back with."""
    redis = await _redis()
    if redis is None:
        return None
    member = _pending_member(account_id, event)
    try:
        await redis.zadd(PENDING_KEY, {member: due})
        return member
    except Exception:
        logger.warning("Could not park an agent account message; answering it without its grace period")
        return None


async def claim(member: str) -> bool:
    """Take a parked message back; ``False`` when someone else already did."""
    redis = await _redis()
    if redis is None:
        return True
    try:
        return bool(await redis.zrem(PENDING_KEY, member))
    except Exception:
        return True


async def overdue(limit: int = 50) -> list[tuple[str, str, AccountEvent]]:
    """Parked messages past their due time by more than the stale margin, claimed for the caller."""
    redis = await _redis()
    if redis is None:
        return []
    cutoff = time.time() - PENDING_STALE_SECONDS
    try:
        members = await redis.zrangebyscore(PENDING_KEY, 0, cutoff, start=0, num=limit)
    except Exception:
        return []
    claimed: list[tuple[str, str, AccountEvent]] = []
    for raw in members or []:
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            if not await redis.zrem(PENDING_KEY, member):
                continue
            data = json.loads(member)
            claimed.append((member, str(data["account_id"]), AccountEvent.from_dict(data["event"])))
        except Exception:
            logger.warning("Dropped an unreadable parked agent account message")
    return claimed


async def forget_account(account_id: str) -> None:
    """Drop the account's poll cursor (its other keys expire on their own)."""
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.delete(_key("cursor", account_id))
    except Exception:
        logger.debug("Could not clear an agent account's state", exc_info=True)


async def read_cursor(account_id: str) -> str | None:
    redis = await _redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(_key("cursor", account_id))
    except Exception:
        return None
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def read_cursors(account_ids: list[str]) -> dict[str, str]:
    """The poll cursors of *account_ids* in one read, the accounts without one left out.

    What ``get_agent_account`` shows as where the account's listening stands
    (a Gmail mailbox: its last history id). Fails open: no Redis, no cursors.
    """
    redis = await _redis()
    if redis is None or not account_ids:
        return {}
    try:
        raws = await redis.mget([_key("cursor", account_id) for account_id in account_ids])
    except Exception:
        logger.debug("Could not read agent account poll cursors", exc_info=True)
        return {}
    out: dict[str, str] = {}
    for account_id, raw in zip(account_ids, raws or [], strict=False):
        if raw is not None:
            out[account_id] = raw.decode() if isinstance(raw, bytes) else str(raw)
    return out


async def write_cursor(account_id: str, cursor: str | None) -> None:
    redis = await _redis()
    if redis is None or not cursor:
        return
    try:
        await redis.set(_key("cursor", account_id), cursor, ex=7 * 24 * 60 * 60)
    except Exception:
        logger.debug("Could not store an agent account poll cursor", exc_info=True)


def _synced_key(account_id: str) -> str:
    return f"agent_account:synced:{account_id}"


async def mark_synced(account_id: str, updated_at: datetime | None) -> None:
    """The account's listening matches its settings as of *updated_at*."""
    redis = await _redis()
    if redis is None:
        return
    value = json.dumps({"updated_at": updated_at.isoformat() if updated_at else None, "at": time.time()})
    try:
        await redis.set(_synced_key(account_id), value, ex=24 * 60 * 60)
    except Exception:
        logger.debug("Could not record an agent account sync", exc_info=True)


async def synced_since(account_id: str, updated_at: datetime | None, within: Any) -> bool:
    """Whether the account was synced after its last change and within *within* (a timedelta)."""
    redis = await _redis()
    if redis is None:
        return False
    try:
        raw = await redis.get(_synced_key(account_id))
        if not raw:
            return False
        data = json.loads(raw)
    except Exception:
        return False
    if data.get("updated_at") != (updated_at.isoformat() if updated_at else None):
        return False
    return time.time() - float(data.get("at") or 0) < within.total_seconds()


async def clear_synced(account_id: str) -> None:
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.delete(_synced_key(account_id))
    except Exception:
        logger.debug("Could not clear an agent account sync marker", exc_info=True)


#: The sorted set of channel threads read for their follow-ups, scored by due time.
FOLLOWED_THREADS_KEY = "agent_account:followed_threads"
#: How often a followed thread is read.
FOLLOW_EVERY_SECONDS = 60


def _followed_member(account_id: str, conversation_id: str, thread_id: str, route: dict[str, Any]) -> str:
    return json.dumps(
        {"account_id": account_id, "conversation_id": conversation_id, "thread_id": thread_id, "route": route},
        sort_keys=True,
    )


async def follow_thread(account_id: str, conversation_id: str, thread_id: str, route: dict[str, Any]) -> None:
    """Read the channel thread the account just answered in for its follow-ups, for as long as it stays active.

    The read cursor of a thread already followed is kept: a second answer
    extends the following, it never skips what the first one had not read.
    """
    redis = await _redis()
    if redis is None:
        return
    now = time.time()
    cursor = _key("followed", account_id, thread_id)
    try:
        pipe = redis.pipeline()
        pipe.set(cursor, str(now), nx=True, ex=THREAD_ACTIVE_TTL_SECONDS)
        pipe.expire(cursor, THREAD_ACTIVE_TTL_SECONDS)
        pipe.zadd(FOLLOWED_THREADS_KEY, {_followed_member(account_id, conversation_id, thread_id, route): now + FOLLOW_EVERY_SECONDS})
        await pipe.execute()
    except Exception:
        logger.debug("Could not follow an agent account thread", exc_info=True)


async def due_followed_threads(limit: int) -> list[tuple[str, str, str, dict[str, Any], float]]:
    """The followed threads due for a read, each claimed by this caller alone.

    ``(account_id, conversation_id, thread_id, route, read_up_to)``; a thread
    whose following ended (its cursor expired) is dropped.
    """
    redis = await _redis()
    if redis is None:
        return []
    try:
        members = await redis.zrangebyscore(FOLLOWED_THREADS_KEY, 0, time.time(), start=0, num=limit)
    except Exception:
        return []
    claimed: list[tuple[str, str, str, dict[str, Any], float]] = []
    for raw in members or []:
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            if not await redis.zrem(FOLLOWED_THREADS_KEY, member):
                continue
            data = json.loads(member)
            read_up_to = await redis.get(_key("followed", data["account_id"], data["thread_id"]))
            if read_up_to is None:
                continue
            claimed.append(
                (data["account_id"], data["conversation_id"], data["thread_id"], data["route"], float(read_up_to))
            )
        except Exception:
            logger.warning("Dropped an unreadable followed agent account thread")
    return claimed


async def followed_thread_read(
    account_id: str, conversation_id: str, thread_id: str, route: dict[str, Any], read_up_to: float
) -> None:
    """The thread was read up to *read_up_to*: read it again shortly while it stays followed."""
    redis = await _redis()
    if redis is None:
        return
    cursor = _key("followed", account_id, thread_id)
    try:
        if not await redis.set(cursor, str(read_up_to), xx=True, keepttl=True):
            return
        await redis.zadd(
            FOLLOWED_THREADS_KEY,
            {_followed_member(account_id, conversation_id, thread_id, route): time.time() + FOLLOW_EVERY_SECONDS},
        )
    except Exception:
        logger.debug("Could not schedule the next read of a followed agent account thread", exc_info=True)


#: How long the last time a provider validated the notification address is remembered.
HANDSHAKE_TTL_SECONDS = 24 * 60 * 60
HANDSHAKE_WRITE_TIMEOUT_SECONDS = 1.0


def _handshake_key(provider: str) -> str:
    return f"agent_account:handshake:{provider}"


async def note_handshake(provider: str) -> None:
    """The provider called the notification address back to validate it.

    Bounded: the provider waits for the validation answer a few seconds only.
    """
    redis = await _redis()
    if redis is None:
        return
    try:
        await asyncio.wait_for(
            redis.set(_handshake_key(provider), str(time.time()), ex=HANDSHAKE_TTL_SECONDS),
            timeout=HANDSHAKE_WRITE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.debug("Could not record an agent account notification handshake", exc_info=True)


async def handshake_since(provider: str, since: float) -> bool | None:
    """Whether the provider validated the notification address after *since*; ``None`` when unknown."""
    redis = await _redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(_handshake_key(provider))
    except Exception:
        return None
    if not raw:
        return False
    try:
        return float(raw) >= since
    except (TypeError, ValueError):
        return None


async def take_lease(name: str, seconds: int) -> bool:
    """A cluster-wide lease, so one process runs a periodic step at a time."""
    redis = await _redis()
    if redis is None:
        return True
    try:
        return bool(await redis.set(f"agent_account:lease:{name}", "1", nx=True, ex=seconds))
    except Exception:
        return True


async def release_lease(name: str) -> None:
    """Give a lease back before it expires: the step it guarded is over."""
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.delete(f"agent_account:lease:{name}")
    except Exception:
        logger.debug("Could not release an agent account lease", exc_info=True)


#: How long a post of any agent account stays recognisable to the other accounts.
AGENT_POST_TTL_SECONDS = 24 * 60 * 60


def _agent_post_key(provider: str, conversation_id: str, message_id: str) -> str:
    return f"agent_account:agent_post:{provider}:{_digest(conversation_id)}:{_digest(message_id)}"


async def mark_agent_post(provider: str, conversation_id: str, message_id: str | None) -> None:
    """Remember a message an agent account posted, for every account of the platform."""
    redis = await _redis()
    if redis is None or not message_id:
        return
    try:
        await redis.set(_agent_post_key(provider, conversation_id, message_id), "1", ex=AGENT_POST_TTL_SECONDS)
    except Exception:
        logger.debug("Could not record an agent account post for the other accounts", exc_info=True)


async def is_agent_post(provider: str, conversation_id: str, message_id: str) -> bool:
    """Whether an agent account posted *message_id*, the account reading it or another one."""
    redis = await _redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_agent_post_key(provider, conversation_id, message_id)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

#: The sorted set of answers whose reactions are read periodically, scored by due time.
REACTION_CHECKS_KEY = "agent_account:reaction_checks"
#: The longest a claimed read keeps the next notifications of an answer from
#: reading it. The holder releases the claim after a short wait
#: (``reactions.COALESCE_WAIT_SECONDS``); this only bounds a holder that died.
REACTION_COALESCE_SECONDS = 30
#: Answers scheduled per Redis call.
SCHEDULE_BATCH = 500
#: How long a provider's refusal of the reaction variant of a subscription
#: stands (``note_reactions_refused``) before a sync asks for it again.
REACTIONS_REFUSED_TTL_SECONDS = 24 * 60 * 60
REACTIONS_REFUSED_DETAIL_CHARS = 600


async def claim_reaction_refresh(account_id: str, conversation_id: str, message_id: str) -> bool:
    """Whether this notification of a changed answer reads it; ``False`` when a read is already due.

    A reaction burst (a whole team agreeing) notifies once per reaction; one
    read after a short wait sees them all. Fails open: without Redis every
    notification reads.
    """
    redis = await _redis()
    if redis is None:
        return True
    try:
        key = _key("reaction_refresh", account_id, conversation_id, message_id)
        return bool(await redis.set(key, "1", nx=True, ex=REACTION_COALESCE_SECONDS))
    except Exception:
        return True


async def release_reaction_refresh(account_id: str, conversation_id: str, message_id: str) -> None:
    """Let the next notification of the answer read it again: called right before a read,
    so a reaction that arrives during the read is never left unread."""
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.delete(_key("reaction_refresh", account_id, conversation_id, message_id))
    except Exception:
        logger.debug("Could not release an agent account reaction read", exc_info=True)


async def schedule_reaction_check(reply_id: str, due: float) -> None:
    """Read the reactions of an answer at *due* (epoch seconds)."""
    await schedule_reaction_checks({reply_id: due})


async def schedule_reaction_checks(due_by_reply: dict[str, float]) -> None:
    """Read the reactions of each answer at its due time (epoch seconds), a later one replacing an earlier."""
    if not due_by_reply:
        return
    redis = await _redis()
    if redis is None:
        return
    items = list(due_by_reply.items())
    try:
        for start in range(0, len(items), SCHEDULE_BATCH):
            await redis.zadd(REACTION_CHECKS_KEY, dict(items[start : start + SCHEDULE_BATCH]))
    except Exception:
        logger.debug("Could not schedule an agent account reaction check", exc_info=True)


async def note_reactions_refused(account_id: str, resource: str, detail: str) -> None:
    """The provider refused to notify the reactions on *resource*: for a while,
    the account's syncs ask it for the new messages alone instead of trying again."""
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.set(
            _key("reactions_refused", account_id, resource),
            detail[:REACTIONS_REFUSED_DETAIL_CHARS] or "refused",
            ex=REACTIONS_REFUSED_TTL_SECONDS,
        )
    except Exception:
        logger.debug("Could not record a refused agent account subscription", exc_info=True)


async def reactions_refused(account_id: str, resource: str) -> str | None:
    """The provider's words when it refused the reactions on *resource* lately, or ``None``.

    ``None`` without Redis too: the sync then asks for them again.
    """
    redis = await _redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(_key("reactions_refused", account_id, resource))
    except Exception:
        return None
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def due_reaction_checks(limit: int) -> list[str]:
    """The answers whose reaction check is due, each claimed by this caller alone."""
    redis = await _redis()
    if redis is None:
        return []
    try:
        due = await redis.zrangebyscore(REACTION_CHECKS_KEY, 0, time.time(), start=0, num=limit)
    except Exception:
        return []
    claimed: list[str] = []
    for member in due:
        reply_id = member.decode() if isinstance(member, bytes) else str(member)
        try:
            if await redis.zrem(REACTION_CHECKS_KEY, member):
                claimed.append(reply_id)
        except Exception:
            logger.debug("Could not claim an agent account reaction check", exc_info=True)
    return claimed

