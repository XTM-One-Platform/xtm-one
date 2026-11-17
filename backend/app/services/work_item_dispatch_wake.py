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

"""Dispatch a work item to its inbox assignment when it becomes dispatchable, not at the next tick.

The work-item inbox (``autonomous._process_work_item_assignments``) runs in the
scheduler tick, once per 60 s window across every process, and dispatches one
item per inbox per tick. An item created just after a tick waited up to 60 s
for its run, and the next item of a busy inbox waited for the tick after the
run ended, every time.

Now the writes that make an item dispatchable nudge a Redis channel after their
commit (:func:`notify_dispatch`):

* an item created ``open`` (``work_item.create_work_item``);
* a status back to ``open`` (a release after a skipped dispatch or a failed
  run, a requeue, a dependency reopen): every ``work_item.status_changed``
  event to ``open`` goes through ``work_item._publish``;
* the end of a run an inbox started (the assignment adapter), which frees the
  serial inbox for its next item;
* an open owned item whose owner, priority, correlation key or due time
  changed (``work_item.update_work_item``).

The pass serves both consumers the tick serves: inbox assignments and flow
``trigger.work_item`` nodes (``flow_triggers.process_work_item_flows``).

Each process listens (:func:`run_wake_loop`); a nudge runs one inbox pass
(:func:`dispatch_pass`) under a short cross-process lock. A nudge that arrives
while another process holds the lock sets :data:`PENDING_KEY`, and the holder
runs one more pass before it lets go, so no nudge is lost to the lock. The pass
is the tick's own function with the tick's own gates (AI on, owner active,
atomic claim, ``skip_if_active``): this is a second trigger of the same pass,
never a second dispatch path. The 60 s tick stays the backstop for a lost
nudge or a Redis outage.

``xtmone_work_item_dispatch_lag_seconds{path}`` measures the wait from the
item's last activity (its creation or release) to its dispatch, per path
(``tick`` or ``wake``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.metrics import get_or_create_histogram
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The Redis channel a dispatchable item nudges.
DISPATCH_WAKE_CHANNEL = "work_items:dispatch"
#: Held by the process running an inbox pass; expires on its own if that process dies mid-pass.
LOCK_KEY = "copilot:work_item_dispatch_lock"
#: Longer than one pass over every inbox, renewed by token after each pass.
LOCK_SECONDS = 120
#: Set by a nudge; the lock holder runs one more pass while it is set.
PENDING_KEY = "copilot:work_item_dispatch_pending"
PENDING_SECONDS = 120
#: Passes one holder runs back to back at most, before leaving the rest to the next nudge or the tick.
MAX_PASSES = 5
#: Buffered nudges one pass consumes: a burst costs one pass.
_MAX_COALESCED = 1000
#: Seconds between two subscribe attempts when Redis is unreachable.
_RESUBSCRIBE_SECONDS = 30.0

_UNLOCK_SCRIPT = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
#: Renew only a lock this holder still owns: once it expired and another process took it, the old holder stops.
_RENEW_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)

DISPATCH_LAG = get_or_create_histogram(
    "xtmone_work_item_dispatch_lag_seconds",
    "Seconds from a work item's last activity (creation, release) to its dispatch to an inbox assignment, by path.",
    ["path"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 300),
)


def observe_lag(item: dict, path: str, *, now: datetime | None = None) -> None:
    """Record the dispatch lag of the serialized *item* (``last_activity_at``, else ``created_at``); never raises."""
    raw = item.get("last_activity_at") or item.get("created_at")
    if not raw:
        return
    try:
        since = datetime.fromisoformat(str(raw))
    except ValueError:
        return
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    seconds = ((now or datetime.now(UTC)) - since).total_seconds()
    DISPATCH_LAG.labels(path=path).observe(max(0.0, seconds))


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def notify_dispatch() -> None:
    """Nudge every process to run an inbox pass now (after the commit that made an item dispatchable); never raises."""
    try:
        redis = await _redis()
        await redis.set(PENDING_KEY, "1", ex=PENDING_SECONDS)
        await redis.publish(DISPATCH_WAKE_CHANNEL, "1")
    except Exception:
        logger.debug("work item dispatch wake not published; the scheduler tick dispatches", exc_info=True)


def reopens(events: Iterable[tuple[str, dict]]) -> bool:
    """Whether *events* (``work_item._publish``'s) put an item back to ``open``."""
    return any(
        name in {"work_item.status_changed", "work_item.claim_released"} and (data or {}).get("status") == "open"
        for name, data in events
    )


async def _default_dispatch() -> int:
    """Both work-item consumers of the tick: inbox assignments, then flow ``trigger.work_item`` nodes.

    The atomic claim keeps the two from ever running one item twice, as in the tick.
    As in the tick, only the inbox consumer is gated on AI availability: the flow
    consumer runs whatever the AI state, and ``flow_triggers.hold_back_if_ai_required``
    holds back a graph that contains an AI node while a deterministic graph runs.
    """
    from app.core.database import async_session
    from app.llm.provider_access import acting_for
    from app.services.agentic_gate import agentic_refusal
    from app.services.autonomous import _process_work_item_assignments
    from app.services.flow_triggers import process_work_item_flows

    async with async_session() as db:
        dispatched = 0
        with acting_for(None):
            if await agentic_refusal(db) is None:
                dispatched += await _process_work_item_assignments(db, path="wake")
            try:
                dispatched += await process_work_item_flows(db)
            except Exception as exc:
                logger.warning("Work item flow dispatch on wake failed: %s", format_exception_message(exc))
                await db.rollback()
        return dispatched


async def dispatch_pass(*, dispatch: Any = None) -> int:
    """Run the inbox pass while a nudge is pending, under the cross-process lock; the items dispatched.

    *dispatch* stands in for the pass itself (tests). Returns 0 without running
    when another process holds the lock: :data:`PENDING_KEY` stays set and that
    holder runs again before it lets go.
    """
    run = dispatch or _default_dispatch
    redis = await _redis()
    token = uuid.uuid4().hex
    if not await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
        return 0
    dispatched = 0
    try:
        for _ in range(MAX_PASSES):
            if not await redis.getdel(PENDING_KEY):
                break
            started = time.monotonic()
            try:
                dispatched += int(await run() or 0)
            except Exception as exc:
                logger.warning("Work item dispatch pass failed: %s", format_exception_message(exc))
                break
            logger.debug("Work item dispatch pass in %.2f s", time.monotonic() - started)
            if not await redis.eval(_RENEW_SCRIPT, 1, LOCK_KEY, token, LOCK_SECONDS):
                # The lock expired during the pass and is another process's now: the rest is its own.
                logger.info("Work item dispatch lock lost after a %.0f s pass", time.monotonic() - started)
                break
    finally:
        with contextlib.suppress(Exception):
            await redis.eval(_UNLOCK_SCRIPT, 1, LOCK_KEY, token)
    return dispatched


async def _subscribe() -> tuple[Any, Any]:
    """The wake channel's subscription and its client."""
    from app.core.redis import open_pubsub, redis_pubsub_client

    client = await redis_pubsub_client()
    return await open_pubsub(client, DISPATCH_WAKE_CHANNEL), client


async def run_wake_loop(*, dispatch: Any = None) -> None:
    """Run an inbox pass on every nudge until cancelled; the scheduler tick covers any gap."""
    from app.core.redis import close_pubsub

    while True:
        try:
            pubsub, client = await _subscribe()
        except Exception as exc:
            logger.warning("Work item dispatch wake unavailable, retrying: %s", format_exception_message(exc))
            await asyncio.sleep(_RESUBSCRIBE_SECONDS)
            continue
        try:
            # A nudge published while this process was not subscribed yet is still pending.
            await dispatch_pass(dispatch=dispatch)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=60.0)
                if message is None:
                    continue
                for _ in range(_MAX_COALESCED):
                    if await pubsub.get_message(ignore_subscribe_messages=True, timeout=0) is None:
                        break
                await dispatch_pass(dispatch=dispatch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Work item dispatch wake lost, resubscribing: %s", format_exception_message(exc))
        finally:
            await close_pubsub(pubsub, client)
        # Closed first: its connection goes back to the pool before the backoff.
        await asyncio.sleep(1.0)
