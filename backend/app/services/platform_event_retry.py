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

"""Platform event deliveries that errored, delivered again (#2101).

:func:`~app.services.platform_events.dispatch_event` delivers an event to each
subscribed assignment in its own sessions. When the database cannot answer
(the pool exhausted by a burst, a connection dropped), the subscriptions read
or one assignment's delivery raises. Before this module the delivery was
logged and gone: no run, no queued row, no count, no retry, while the other
subscribers of the same event still got their run, so the loss was invisible.

Now an errored delivery is recorded here, one entry per (event, assignment) -
or per event when the subscriptions themselves were unreadable - in a Redis
sorted set scored by its due time, and the scheduler tick
(:func:`process_due`) delivers it again through the same bounded dispatch gate
(#2084), with back-off (:data:`RETRY_DELAYS_SECONDS`). A redelivery is safe:
the run of an event carries ``dedup_key = platform_event:<event id>:<assignment
id>``, so a delivery that created its run before it raised is answered
"duplicate", never a second run.

A delivery still failing after its last attempt is abandoned and said so: it
is counted on the assignment (``events_filtered_count``) with the reason
``delivery failed: <error>`` in its dropped-event reasons (``get_assignment``
"Dropped by reason"), and logged as an error. An entry that cannot be stored
(no Redis) is counted the same way at once, never dropped silently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

PENDING_KEY = "platform_event:delivery_retry:pending"
ENTRY_KEY_PREFIX = "platform_event:delivery_retry:entry:"
#: The delay before each further delivery: half a minute, then 2, 5, 15 and 30 minutes.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (30, 120, 300, 900, 1800)
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS)
#: An entry outlives its last due time by this much (a scheduler down for a while).
ENTRY_TTL_SECONDS = 2 * 3600
#: Deliveries taken per tick.
BATCH = 50
#: A payload over this size is not kept (an event payload is ids and a few short fields).
MAX_PAYLOAD_BYTES = 256 * 1024
#: The prefix of the dropped-event reason an abandoned delivery is counted under.
ABANDONED_REASON_PREFIX = "delivery failed"
_REDIS_TIMEOUT_SECONDS = 0.5


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Platform event retry store unavailable: %s", format_exception_message(exc))
        return None


def entry_id(name: str, event_id: object, assignment_id: object) -> str:
    """One event and one assignment (``None``: every subscriber, the subscriptions were unreadable)."""
    raw = f"{name}|{event_id}|{assignment_id or '*'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def abandoned_reason(error: str) -> str:
    """The dropped-event reason an abandoned delivery is counted under: one line, the error's first words."""
    text = " ".join(str(error or "unknown error").split())
    return f"{ABANDONED_REASON_PREFIX}: {text[:100]}"


async def record_abandoned(assignment_id: object, error: str) -> None:
    """Count one delivery of *assignment_id* given up on, with its reason (best effort, never raises)."""
    if assignment_id is None:
        return
    from app.services.integration_event_filters import record_filter_reasons, record_filtered_events

    try:
        await record_filtered_events(assignment_id, 1)
        await record_filter_reasons(assignment_id, {abandoned_reason(error): 1})
    except Exception as exc:
        logger.warning("Could not count the abandoned delivery of assignment %s: %r", assignment_id, exc)


async def schedule(
    name: str,
    payload: Mapping[str, Any],
    *,
    assignment_id: object,
    attempt: int,
    error: str,
) -> bool:
    """Record a delivery that errored for a further try; ``False`` when given up on (then counted).

    *attempt* is the number of tries already made (1 after the first
    delivery). Past :data:`MAX_ATTEMPTS`, or when the entry cannot be stored,
    the delivery is abandoned: counted on the assignment and logged as an error.
    """
    if attempt > MAX_ATTEMPTS:
        logger.error(
            "Platform event %s %s abandoned for %s after %d attempts: %s",
            name,
            payload.get("event_id"),
            f"assignment {assignment_id}" if assignment_id else "every subscriber",
            attempt,
            error,
        )
        await record_abandoned(assignment_id, error)
        return False
    entry = {
        "name": name,
        "payload": dict(payload),
        "assignment_id": str(assignment_id) if assignment_id else None,
        "attempt": attempt,
        "error": str(error)[:500],
    }
    raw = json.dumps(entry, default=str)
    stored = False
    if len(raw) <= MAX_PAYLOAD_BYTES:
        redis = await _redis()
        if redis is not None:
            member = entry_id(name, payload.get("event_id"), assignment_id)
            due = time.time() + RETRY_DELAYS_SECONDS[max(attempt - 1, 0)]
            try:
                await asyncio.wait_for(
                    redis.set(f"{ENTRY_KEY_PREFIX}{member}", raw, ex=ENTRY_TTL_SECONDS + int(due - time.time())),
                    _REDIS_TIMEOUT_SECONDS,
                )
                await asyncio.wait_for(redis.zadd(PENDING_KEY, {member: due}), _REDIS_TIMEOUT_SECONDS)
                stored = True
            except Exception as exc:
                logger.info("Platform event retry not stored: %s", format_exception_message(exc))
    if not stored:
        logger.error(
            "Platform event %s %s not delivered to %s and not kept for a retry: %s",
            name,
            payload.get("event_id"),
            f"assignment {assignment_id}" if assignment_id else "every subscriber",
            error,
        )
        await record_abandoned(assignment_id, error)
        return False
    logger.warning(
        "Platform event %s %s not delivered to %s (attempt %d of %d), retried later: %s",
        name,
        payload.get("event_id"),
        f"assignment {assignment_id}" if assignment_id else "every subscriber",
        attempt,
        MAX_ATTEMPTS,
        error,
    )
    return True


async def _claim_due(limit: int = BATCH) -> list[dict[str, Any]]:
    """The due entries, each taken by exactly one caller."""
    redis = await _redis()
    if redis is None:
        return []
    try:
        members = await redis.zrangebyscore(PENDING_KEY, 0, time.time(), start=0, num=limit)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for raw_member in members or []:
        member = raw_member.decode() if isinstance(raw_member, bytes) else str(raw_member)
        try:
            if not await redis.zrem(PENDING_KEY, member):
                continue  # another worker took it
            raw = await redis.get(f"{ENTRY_KEY_PREFIX}{member}")
            await redis.delete(f"{ENTRY_KEY_PREFIX}{member}")
            if raw:
                entry = json.loads(raw)
                if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
                    entries.append(entry)
        except Exception:
            logger.warning("Platform event retry: dropped an unreadable entry")
    return entries


async def process_due() -> int:
    """Deliver the due entries again; the deliveries made (a run, a queued run, a duplicate or a note)."""
    entries = await _claim_due()
    if not entries:
        return 0
    from app.services.platform_events import redeliver

    delivered = 0
    for entry in entries:
        try:
            delivered += await redeliver(
                str(entry["name"]),
                entry["payload"],
                assignment_id=entry.get("assignment_id"),
                attempt=int(entry.get("attempt") or 1),
            )
        except Exception as exc:
            logger.error(
                "Platform event retry of %s failed: %s",
                entry.get("name"),
                format_exception_message(exc),
                exc_info=True,
            )
    return delivered
