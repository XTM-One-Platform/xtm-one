# SPDX-License-Identifier: Apache-2.0

"""Queued intake of the platform signal source webhook (#2584).

An OpenCTI catch-up delivers thousands of created or updated objects an hour
to ``receive_signal_source_webhook``. Recording each one on the request path
held a database connection per delivery, exhausted the web pool and froze the
UI, and the per-source throttle refused most deliveries with 429 (OpenCTI
notifiers never retry, so those signals were lost).

The route now authenticates the delivery, hands it to :func:`enqueue` and
answers 202 at once. The queue lives in Redis, so it survives a restart:

- ``QUEUE_KEY`` is a list of entry keys, oldest first;
- ``ENTRIES_KEY`` is a hash of entry key to the delivery. Two deliveries about
  the same OpenCTI object (same source, same internal id) share one entry key:
  the later one replaces the stored delivery and is not queued twice, so a
  burst of updates of one object becomes one signal update (coalescing).

:func:`run_drain_loop` runs in every web process; each of its
``ctem_signal_intake_concurrency`` slots pops one entry atomically and records
it through the same services as before (``ctem_signal_listeners.note_event``
and the OpenCTI / OpenAEV ones), on the intake engine of
``app.core.database.intake_session``: its own connection budget, never the
web pool. What is listened to and the signal logic do not change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from prometheus_client import Gauge

logger = logging.getLogger(__name__)

QUEUE_KEY = "ctem:signal-intake:queue"
ENTRIES_KEY = "ctem:signal-intake:entries"

QUEUE_DEPTH_GAUGE = Gauge(
    "xtmone_ctem_signal_intake_queue_depth",
    "Signal source deliveries accepted and waiting to be recorded",
)
COALESCED_GAUGE = Gauge(
    "xtmone_ctem_signal_intake_coalesced",
    "Signal source deliveries folded into a queued update of the same object since the process started",
)

#: Store the delivery; queue its key only when it was not already waiting. Answers [queued, depth].
_ENQUEUE_LUA = """
local added = redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
if added == 1 then
  redis.call('RPUSH', KEYS[1], ARGV[1])
end
return {added, redis.call('LLEN', KEYS[1])}
"""

#: Pop the oldest key and take its delivery in one step, so two drainers never record one entry.
_POP_LUA = """
local key = redis.call('LPOP', KEYS[1])
if not key then
  return false
end
local value = redis.call('HGET', KEYS[2], key)
redis.call('HDEL', KEYS[2], key)
return value
"""

#: A growing queue is logged once per this many seconds at most.
_WARN_INTERVAL_SECONDS = 60
#: A queue deeper than this is logged as growing.
_WARN_DEPTH = 1000
_last_warning = 0.0
_coalesced = 0


@dataclass(frozen=True)
class Delivery:
    """One verified delivery of a platform signal source, as the drain records it."""

    provider: str
    event_key: str
    payload: dict[str, Any]
    verified: bool
    integration_id: str
    source_id: str

    def as_json(self) -> str:
        return json.dumps(
            {
                "provider": self.provider,
                "event_key": self.event_key,
                "payload": self.payload,
                "verified": self.verified,
                "integration_id": self.integration_id,
                "source_id": self.source_id,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Delivery | None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            return None
        return cls(
            provider=str(data.get("provider") or ""),
            event_key=str(data.get("event_key") or ""),
            payload=data["payload"],
            verified=bool(data.get("verified")),
            integration_id=str(data.get("integration_id") or ""),
            source_id=str(data.get("source_id") or ""),
        )


def entry_key(delivery: Delivery) -> str:
    """The queue key of *delivery*: shared by every update of one OpenCTI object of one source.

    A delete, an OpenAEV or OpenCRQ delivery, or an OpenCTI one without an
    object id gets a key of its own: never folded into another.
    """
    if delivery.provider == "opencti" and "delete" not in delivery.event_key.casefold():
        from app.services.ctem_signal_listeners import opencti_internal_id

        data = delivery.payload.get("data")
        object_id = opencti_internal_id(data) if isinstance(data, Mapping) else ""
        if object_id:
            return f"{delivery.source_id}:{delivery.event_key}:{object_id}"
    return f"{delivery.source_id}:once:{uuid.uuid4().hex}"


def _note_depth(depth: int) -> None:
    global _last_warning
    QUEUE_DEPTH_GAUGE.set(depth)
    now = time.monotonic()
    if depth >= _WARN_DEPTH and now - _last_warning >= _WARN_INTERVAL_SECONDS:
        _last_warning = now
        logger.warning("CTEM signal intake queue is growing: %d deliveries waiting", depth)


async def enqueue(delivery: Delivery) -> bool:
    """Queue *delivery*; ``False`` when Redis is unreachable (the caller records it inline)."""
    global _coalesced
    try:
        from app.core.redis import redis_client

        redis: Any = await redis_client()
        added, depth = await redis.eval(
            _ENQUEUE_LUA, 2, QUEUE_KEY, ENTRIES_KEY, entry_key(delivery), delivery.as_json()
        )
    except Exception as exc:
        logger.warning("CTEM signal intake queue unreachable, recording inline: %r", exc)
        return False
    if not int(added):
        _coalesced += 1
        COALESCED_GAUGE.set(_coalesced)
    _note_depth(int(depth))
    return True


async def depth() -> int | None:
    """Deliveries waiting; ``None`` when Redis is unreachable."""
    try:
        from app.core.redis import redis_client

        redis: Any = await redis_client()
        value = int(await redis.llen(QUEUE_KEY))
    except Exception:
        return None
    QUEUE_DEPTH_GAUGE.set(value)
    return value


async def _pop() -> Delivery | None | Literal[False]:
    """The next delivery; ``False`` when the queue is empty, ``None`` for an unreadable entry."""
    from app.core.redis import redis_client

    redis: Any = await redis_client()
    raw = await redis.eval(_POP_LUA, 2, QUEUE_KEY, ENTRIES_KEY)
    if raw is None or raw is False:
        return False
    return Delivery.from_json(raw)


async def record(delivery: Delivery, *, session_factory: Any = None) -> bool:
    """Record one delivery as the route used to; never raises. ``True`` when a signal was recorded."""
    from app.services import ctem_analyst_mark, ctem_openaev_events, ctem_signal_listeners

    try:
        if delivery.provider == "openaev":
            await ctem_openaev_events.stamp_reachability_assertion(
                delivery.event_key,
                delivery.payload,
                integration_id=delivery.integration_id,
                verified=delivery.verified,
            )
        recorded = await ctem_signal_listeners.note_event(
            delivery.provider,
            delivery.event_key,
            delivery.payload,
            verified=delivery.verified,
            session_factory=session_factory,
        )
        if delivery.provider == "opencti":
            await ctem_analyst_mark.note_event(delivery.provider, delivery.payload, verified=delivery.verified)
        if delivery.provider == "openaev":
            await ctem_openaev_events.note_event(delivery.event_key, delivery.payload, verified=delivery.verified)
        return bool(recorded)
    except Exception:
        logger.warning("CTEM signal intake: a %s delivery was not recorded", delivery.provider, exc_info=True)
        return False


async def _drain_slot(slot: int, *, idle_seconds: float) -> None:
    from app.core.database import intake_session

    while True:
        try:
            delivery = await _pop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("CTEM signal intake slot %d cannot read the queue: %r", slot, exc)
            await asyncio.sleep(max(idle_seconds, 5.0))
            continue
        if delivery is False:
            await asyncio.sleep(idle_seconds)
            continue
        if delivery is None:
            logger.warning("CTEM signal intake: an unreadable queue entry was dropped")
            continue
        await record(delivery, session_factory=intake_session)


async def run_drain_loop(*, concurrency: int | None = None, idle_seconds: float = 0.5) -> None:
    """Drain the intake queue until cancelled, *concurrency* deliveries at a time."""
    from app.core.config import settings

    slots = max(1, int(concurrency if concurrency is not None else settings.ctem_signal_intake_concurrency))
    tasks = [
        asyncio.create_task(_drain_slot(slot, idle_seconds=idle_seconds), name=f"ctem-signal-intake-{slot}")
        for slot in range(slots)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
