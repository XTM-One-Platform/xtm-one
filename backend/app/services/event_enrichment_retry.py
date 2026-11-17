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

"""Deliveries a trigger filter could not judge, evaluated again a little later.

A filter naming the ``enriched.`` facts (``services/event_enrichment.py``)
drops a delivery whose facts could not be read: a filter written to narrow
never widens on an outage. For most events that is the end of it. For a CI
completion it is not acceptable: the delivery that reports the LAST check of a
head finished is the only one that can turn ``enriched.head.checks.all_green``
true, and the provider does not deliver it again. A slow provider or a worker
rolled out during the read then leaves the head green with nothing woken.

So when the read failed for a reason a second read may not meet (a timeout, a
provider error; never a :class:`PermanentEnrichmentError`) and the provider
marks the event retryable (``EventEnricher.retryable``), the receiver records
the delivery here for that one assignment, instead of dropping it for good.
The scheduler tick (``services/autonomous.py``) takes the due ones: it reads
the assignment afresh (enabled, same provider, its current filter), reads the
facts again, and on a match triggers the assignment as the receiver would
have. At most :data:`MAX_ATTEMPTS` further reads, :data:`RETRY_DELAYS_SECONDS`
apart; past them the delivery is dropped with its last reason, counted like
any filtered delivery.

Kept in Redis (a sorted set scored by due time, one member per assignment and
delivery, :data:`ENTRY_TTL_SECONDS` at most): Redis down, nothing is deferred
and the delivery is dropped as before.

A delivery can also be lost before it is read at all: the last check of a head
completes while the workers are replaced (a rollout, a node drain) and its
delivery is never processed, so no read failed and nothing was deferred. So a
CI completion the filter dropped while its head still had checks running
(``enriched.head.checks.all_completed`` false) WATCHES that head for that one
assignment (:func:`watch`, one entry per assignment and head, the latest
delivery kept): every :data:`WATCH_INTERVAL_SECONDS` the scheduler tick
(:func:`process_watched_heads`) reads the head's facts afresh and evaluates the
assignment's current filter on the watched delivery. A head the filter now
admits triggers the assignment once; a head that finished without matching (red,
or a filter miss) ends the watch; a head still running is watched again, for
:data:`WATCH_TTL_SECONDS` at most. A delivery the receiver admitted marks the
head admitted for the assignment (:func:`mark_admitted`), so the head that was
woken by its real last delivery is never woken twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

PENDING_KEY = "integration_event:enrichment_retry:pending"
ENTRY_KEY_PREFIX = "integration_event:enrichment_retry:entry:"
#: The delay before each further read: the first one-minute tick, then three minutes later.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 180)
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS)
ENTRY_TTL_SECONDS = 1800
#: Deliveries taken per tick.
BATCH = 50
#: A payload over this size is not deferred (a CI completion is a few tens of KB).
MAX_PAYLOAD_BYTES = 512 * 1024
_REDIS_TIMEOUT_SECONDS = 0.5

WATCH_KEY = "integration_event:head_watch:pending"
WATCH_ENTRY_PREFIX = "integration_event:head_watch:entry:"
ADMITTED_PREFIX = "integration_event:head_watch:admitted:"
#: How often a watched head is read again.
WATCH_INTERVAL_SECONDS = 180
#: How long a head is watched after its first unfinished completion.
WATCH_TTL_SECONDS = 2 * 3600
#: How long an admitted head is remembered for an assignment.
ADMITTED_TTL_SECONDS = 6 * 3600

HELD_KEY = "integration_event:held_by_lease:pending"
HELD_ENTRY_PREFIX = "integration_event:held_by_lease:entry:"
#: How often the holder of a lease an event stood down on is read again.
HELD_INTERVAL_SECONDS = 60
#: How long an event waits for the lease holder to end before it is dropped.
HELD_TTL_SECONDS = 2 * 3600

REDELIVER_KEY = "integration_event:redeliver:pending"
REDELIVER_ENTRY_PREFIX = "integration_event:redeliver:entry:"
REDELIVERED_PREFIX = "integration_event:redeliver:done:"
#: ``trigger_config`` key: an admitted CI head is delivered ONCE more after
#: this many seconds when the filter still admits it (the end of a window a
#: run stood down on, a review or a bot thread; #11988).
REDELIVER_CONFIG = "redeliver_after_seconds"
REDELIVER_MIN_SECONDS = 60
REDELIVER_MAX_SECONDS = 2 * 3600


def redeliver_after(trigger_config: Any) -> int | None:
    """The assignment's ``redeliver_after_seconds``, bounded; ``None`` when unset or not a number."""
    if not isinstance(trigger_config, dict):
        return None
    raw = trigger_config.get(REDELIVER_CONFIG)
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        seconds = int(float(raw))
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return max(REDELIVER_MIN_SECONDS, min(REDELIVER_MAX_SECONDS, seconds))


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Enrichment retry store unavailable: %s", format_exception_message(exc))
        return None


def entry_id(assignment_id: Any, event_key: str, payload: dict[str, Any]) -> str:
    """One assignment and one delivery: a redelivery of the same body is the same entry."""
    body = json.dumps(payload, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(f"{assignment_id}|{event_key}|".encode() + body).hexdigest()[:32]
    return digest


async def defer(
    *,
    assignment_id: Any,
    provider: str,
    event_key: str,
    payload: dict[str, Any],
    reason: str,
    receiver_integration_id: Any = None,
    trigger_context: dict[str, Any] | None = None,
    attempt: int = 0,
) -> bool:
    """Record the delivery for a further read; ``False`` when it is not (attempts spent, too large, no store)."""
    if attempt >= MAX_ATTEMPTS or not isinstance(payload, dict):
        return False
    entry = {
        "assignment_id": str(assignment_id),
        "provider": provider,
        "event_key": event_key,
        "payload": payload,
        "receiver_integration_id": str(receiver_integration_id) if receiver_integration_id else None,
        "trigger_context": trigger_context or {},
        "attempt": attempt + 1,
        "first_reason": reason,
    }
    raw = json.dumps(entry, default=str)
    if len(raw) > MAX_PAYLOAD_BYTES:
        logger.info("Enrichment retry: delivery %s for assignment %s too large to defer", event_key, assignment_id)
        return False
    redis = await _redis()
    if redis is None:
        return False
    member = entry_id(assignment_id, event_key, payload)
    due = time.time() + RETRY_DELAYS_SECONDS[attempt]
    try:
        await asyncio.wait_for(
            redis.set(f"{ENTRY_KEY_PREFIX}{member}", raw, ex=ENTRY_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(redis.zadd(PENDING_KEY, {member: due}), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.info("Enrichment retry: delivery not deferred: %s", format_exception_message(exc))
        return False
    logger.info(
        "Enrichment retry: %s %s deferred for assignment %s (attempt %d of %d): %s",
        provider,
        event_key,
        assignment_id,
        attempt + 1,
        MAX_ATTEMPTS,
        reason,
    )
    return True


async def _claim_due(
    limit: int = BATCH, *, pending_key: str = PENDING_KEY, entry_prefix: str = ENTRY_KEY_PREFIX
) -> list[dict[str, Any]]:
    """The due entries, each taken by exactly one caller."""
    redis = await _redis()
    if redis is None:
        return []
    try:
        members = await redis.zrangebyscore(pending_key, 0, time.time(), start=0, num=limit)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for raw_member in members or []:
        member = raw_member.decode() if isinstance(raw_member, bytes) else str(raw_member)
        try:
            if not await redis.zrem(pending_key, member):
                continue  # another worker took it
            raw = await redis.get(f"{entry_prefix}{member}")
            await redis.delete(f"{entry_prefix}{member}")
            if raw:
                entry = json.loads(raw)
                if isinstance(entry, dict):
                    entries.append(entry)
        except Exception:
            logger.warning("Enrichment retry: dropped an unreadable deferred delivery")
    return entries


async def _evaluate(db: Any, entry: dict[str, Any]) -> str | None:
    """Trigger the entry's assignment when its current filter now admits the delivery; the run id."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.services import event_enrichment, integration_event_filters
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError, owner_not_deactivated

    assignment_id = safe_uuid(entry.get("assignment_id"))
    provider = entry.get("provider")
    event_key = entry.get("event_key")
    payload = entry.get("payload")
    if assignment_id is None or not isinstance(provider, str) or not isinstance(event_key, str):
        return None
    if not isinstance(payload, dict):
        return None
    wf = (
        await db.execute(
            select(Assignment).where(
                Assignment.id == assignment_id,
                Assignment.enabled.is_(True),
                owner_not_deactivated(),
            )
        )
    ).scalar_one_or_none()
    if wf is None or wf.trigger_type != "integration_event":
        return None
    config = wf.trigger_config if isinstance(wf.trigger_config, dict) else {}
    if config.get("provider") != provider:
        return None
    agent = await db.get(Agent, wf.agent_id)
    if agent is None or not agent.enabled:
        return None
    # The filter may have changed since the deferral: a plain key that drops
    # the delivery now ends it without a further read of the facts.
    reason = integration_event_filters.plain_filter_reason(
        config.get("event") or "", payload, integration_event_filters.trigger_filter(config)
    )
    if reason is not None:
        await integration_event_filters.record_filtered_events(assignment_id, 1)
        await integration_event_filters.record_filter_reasons(
            str(assignment_id), {integration_event_filters.reason_label(reason): 1}
        )
        return None
    facts = await event_enrichment.enrichment_for(
        db,
        provider,
        event_key,
        payload,
        trigger_config=config,
        agent=agent,
        receiver_integration_id=entry.get("receiver_integration_id"),
    )
    reason = integration_event_filters.event_filter_reason(
        config.get("event") or "",
        event_enrichment.with_enrichment(payload, facts),
        integration_event_filters.trigger_filter(config),
    )
    if reason is not None:
        attempt = int(entry.get("attempt") or MAX_ATTEMPTS)
        if event_enrichment.enrichment_retryable(facts) and await defer(
            assignment_id=assignment_id,
            provider=provider,
            event_key=event_key,
            payload=payload,
            reason=reason,
            receiver_integration_id=entry.get("receiver_integration_id"),
            trigger_context=entry.get("trigger_context"),
            attempt=attempt,
        ):
            return None
        logger.info(
            "Enrichment retry: %s %s dropped for assignment %s after %d further read(s): %s",
            provider,
            event_key,
            assignment_id,
            attempt,
            reason,
        )
        await integration_event_filters.record_filtered_events(assignment_id, 1)
        await integration_event_filters.record_filter_reasons(
            str(assignment_id), {integration_event_filters.reason_label(reason): 1}
        )
        return None

    from app.services import assignment_engine

    trigger_context = dict(entry.get("trigger_context") or {})
    trigger_context.setdefault("type", "webhook")
    trigger_context.setdefault("provider", provider)
    trigger_context.setdefault("event_key", event_key)
    trigger_context["body"] = payload
    trigger_context["enrichment_retry"] = {
        "attempt": entry.get("attempt"),
        "first_reason": entry.get("first_reason"),
    }
    subject = event_enrichment.watch_subject(provider, event_key, payload)
    if subject is not None:
        await mark_admitted(assignment_id, subject)
    try:
        run_id = await assignment_engine.trigger_assignment(
            assignment=wf,
            agent=agent,
            payload=payload,
            db=db,
            trigger_context=trigger_context,
            skip_if_active=True,
        )
        if run_id is None:
            run_id = await assignment_engine.queue_busy_pushed_event(
                wf, agent, payload, db=db, trigger_context=trigger_context
            )
    except AssignmentOwnerDeactivatedError:
        return None
    if run_id is not None:
        logger.info(
            "Enrichment retry: %s %s triggered assignment %s on attempt %s: run %s",
            provider,
            event_key,
            assignment_id,
            entry.get("attempt"),
            run_id,
        )
    return run_id


def _watch_member(assignment_id: Any, subject: str) -> str:
    return hashlib.sha256(f"{assignment_id}|{subject}".encode()).hexdigest()[:32]


async def is_admitted(assignment_id: Any, subject: str) -> bool:
    """Whether a delivery on *subject* was already admitted for the assignment (no store: false)."""
    redis = await _redis()
    if redis is None:
        return False
    try:
        return bool(
            await asyncio.wait_for(
                redis.get(f"{ADMITTED_PREFIX}{_watch_member(assignment_id, subject)}"), _REDIS_TIMEOUT_SECONDS
            )
        )
    except Exception:
        return False


async def mark_admitted(assignment_id: Any, subject: str) -> None:
    """Record that the filter admitted a delivery on *subject* for the assignment: its watch ends."""
    redis = await _redis()
    if redis is None:
        return
    member = _watch_member(assignment_id, subject)
    try:
        await asyncio.wait_for(
            redis.set(f"{ADMITTED_PREFIX}{member}", "1", ex=ADMITTED_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(redis.zrem(WATCH_KEY, member), _REDIS_TIMEOUT_SECONDS)
        await asyncio.wait_for(redis.delete(f"{WATCH_ENTRY_PREFIX}{member}"), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Head watch: admission not recorded: %s", format_exception_message(exc))


async def watch(
    *,
    assignment_id: Any,
    provider: str,
    event_key: str,
    payload: dict[str, Any],
    subject: str,
    receiver_integration_id: Any = None,
    trigger_context: dict[str, Any] | None = None,
) -> bool:
    """Watch *subject* for the assignment: its facts are read again until they settle; ``False`` when not.

    One entry per assignment and subject: a later delivery on the same head
    replaces the payload kept and the next read time; the first watch time is
    kept, so a head is never watched past :data:`WATCH_TTL_SECONDS`.
    """
    if not isinstance(payload, dict) or not subject:
        return False
    redis = await _redis()
    if redis is None:
        return False
    member = _watch_member(assignment_id, subject)
    now = time.time()
    try:
        if await asyncio.wait_for(redis.get(f"{ADMITTED_PREFIX}{member}"), _REDIS_TIMEOUT_SECONDS):
            return False
        previous = await asyncio.wait_for(redis.get(f"{WATCH_ENTRY_PREFIX}{member}"), _REDIS_TIMEOUT_SECONDS)
        since = now
        if previous:
            old = json.loads(previous)
            if isinstance(old, dict) and isinstance(old.get("watched_since"), int | float):
                since = float(old["watched_since"])
        if now - since >= WATCH_TTL_SECONDS:
            return False
        entry = {
            "assignment_id": str(assignment_id),
            "provider": provider,
            "event_key": event_key,
            "payload": payload,
            "subject": subject,
            "receiver_integration_id": str(receiver_integration_id) if receiver_integration_id else None,
            "trigger_context": trigger_context or {},
            "watched_since": since,
        }
        raw = json.dumps(entry, default=str)
        if len(raw) > MAX_PAYLOAD_BYTES:
            return False
        await asyncio.wait_for(
            redis.set(f"{WATCH_ENTRY_PREFIX}{member}", raw, ex=WATCH_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(redis.zadd(WATCH_KEY, {member: now + WATCH_INTERVAL_SECONDS}), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.info("Head watch: %s not watched: %s", subject, format_exception_message(exc))
        return False
    return True


async def hold_for_lease(
    *,
    assignment_id: Any,
    provider: str,
    event_key: str,
    payload: dict[str, Any],
    subject: str,
    holder_run_id: str,
    trigger_context: dict[str, Any] | None = None,
) -> bool:
    """Keep a CI event whose run stood down on a LIVE lease holder; ``False`` when it is not kept.

    A run its gate skipped because another live run holds the lease it reads
    (``ActivationCheckOutcome.live_holder``) would lose the event: the green
    of a head is delivered once (#11988). The event is kept for the
    assignment, one entry per assignment and head (a second event on the same
    sha replaces the first, so one delivery follows), and
    :func:`process_held_events` delivers it again once the holder run ended,
    the filter evaluated on the head's fresh facts. The first hold time is
    kept: an event never waits past :data:`HELD_TTL_SECONDS`.
    """
    if not isinstance(payload, dict) or not subject or not holder_run_id:
        return False
    redis = await _redis()
    if redis is None:
        return False
    member = _watch_member(assignment_id, subject)
    now = time.time()
    try:
        previous = await asyncio.wait_for(redis.get(f"{HELD_ENTRY_PREFIX}{member}"), _REDIS_TIMEOUT_SECONDS)
        since = now
        if previous:
            old = json.loads(previous)
            if isinstance(old, dict) and isinstance(old.get("held_since"), int | float):
                since = float(old["held_since"])
        if now - since >= HELD_TTL_SECONDS:
            return False
        context = {k: v for k, v in (trigger_context or {}).items() if k not in ("body", "held_by_lease")}
        entry = {
            "assignment_id": str(assignment_id),
            "provider": provider,
            "event_key": event_key,
            "payload": payload,
            "subject": subject,
            "holder_run_id": str(holder_run_id),
            "receiver_integration_id": context.get("receiver_integration_id") or context.get("integration_id"),
            "trigger_context": context,
            "held_since": since,
        }
        raw = json.dumps(entry, default=str)
        if len(raw) > MAX_PAYLOAD_BYTES:
            return False
        await asyncio.wait_for(
            redis.set(f"{HELD_ENTRY_PREFIX}{member}", raw, ex=HELD_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(redis.zadd(HELD_KEY, {member: now + HELD_INTERVAL_SECONDS}), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.info("Lease hold: %s not kept: %s", subject, format_exception_message(exc))
        return False
    logger.info(
        "Lease hold: %s %s kept for assignment %s until run %s ends", provider, subject, assignment_id, holder_run_id
    )
    return True


async def schedule_redelivery(
    *,
    assignment_id: Any,
    provider: str,
    event_key: str,
    payload: dict[str, Any],
    subject: str,
    delay_seconds: int,
    trigger_context: dict[str, Any] | None = None,
) -> bool:
    """Deliver an admitted head once more after *delay_seconds*; ``False`` when not scheduled.

    One re-delivery per assignment and head, ever: a later admission of the
    same head (another check finishing) never schedules a second one, so a
    head is woken at most twice (its admission and the window end).
    """
    if not isinstance(payload, dict) or not subject or delay_seconds <= 0:
        return False
    redis = await _redis()
    if redis is None:
        return False
    member = _watch_member(assignment_id, subject)
    try:
        first = await asyncio.wait_for(
            redis.set(f"{REDELIVERED_PREFIX}{member}", "1", ex=ADMITTED_TTL_SECONDS, nx=True),
            _REDIS_TIMEOUT_SECONDS,
        )
        if not first:
            return False
        context = {k: v for k, v in (trigger_context or {}).items() if k != "body"}
        entry = {
            "assignment_id": str(assignment_id),
            "provider": provider,
            "event_key": event_key,
            "payload": payload,
            "subject": subject,
            "receiver_integration_id": context.get("receiver_integration_id") or context.get("integration_id"),
            "trigger_context": {**context, "redelivery": {"after_seconds": delay_seconds}},
            "watched_since": time.time(),
        }
        raw = json.dumps(entry, default=str)
        if len(raw) > MAX_PAYLOAD_BYTES:
            return False
        await asyncio.wait_for(
            redis.set(f"{REDELIVER_ENTRY_PREFIX}{member}", raw, ex=delay_seconds + ENTRY_TTL_SECONDS),
            _REDIS_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(redis.zadd(REDELIVER_KEY, {member: time.time() + delay_seconds}), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.info("Redelivery: %s not scheduled: %s", subject, format_exception_message(exc))
        return False
    return True


async def process_redeliveries() -> int:
    """Deliver the due admitted heads once more when the filter still admits them; the runs started."""
    entries = await _claim_due(pending_key=REDELIVER_KEY, entry_prefix=REDELIVER_ENTRY_PREFIX)
    if not entries:
        return 0
    from app.core.database import async_session

    started = 0
    for entry in entries:
        try:
            async with async_session() as db:
                run_id = await _evaluate_watch(db, entry, honour_admitted=False)
                await db.commit()
            if run_id is not None:
                started += 1
        except Exception as exc:
            logger.error(
                "Redelivery of assignment %s failed: %s",
                entry.get("assignment_id"),
                format_exception_message(exc),
                exc_info=True,
            )
    return started


async def _holder_alive(db: Any, holder_run_id: Any) -> bool:
    """Whether the run holding the lease still lives (an unreadable state reads as alive: read again later)."""
    rid = safe_uuid(holder_run_id)
    if rid is None:
        return False
    from app.services.work_item import holding_runs

    try:
        holder = (await holding_runs(db, [rid])).get(rid)
    except Exception as exc:
        logger.info("Lease hold: holder %s not read: %s", holder_run_id, format_exception_message(exc))
        return True
    return bool(holder is not None and holder.alive)


async def _evaluate_held(db: Any, entry: dict[str, Any]) -> str | None:
    """Deliver a held event again once its lease holder ended; the run id."""
    subject = entry.get("subject")
    if not isinstance(subject, str):
        return None
    if await _holder_alive(db, entry.get("holder_run_id")):
        # Still working: kept, read again at the next interval.
        await hold_for_lease(
            assignment_id=entry.get("assignment_id"),
            provider=str(entry.get("provider") or ""),
            event_key=str(entry.get("event_key") or ""),
            payload=entry.get("payload") or {},
            subject=subject,
            holder_run_id=str(entry.get("holder_run_id") or ""),
            trigger_context=entry.get("trigger_context"),
        )
        return None
    context = dict(entry.get("trigger_context") or {})
    context["held_by_lease"] = {"holder_run_id": entry.get("holder_run_id"), "held_since": entry.get("held_since")}
    return await _evaluate_watch(
        db,
        {**entry, "trigger_context": context, "watched_since": entry.get("held_since")},
        honour_admitted=False,
    )


async def process_held_events() -> int:
    """Deliver again the due events held by a lease whose holder ended; the number of runs they started."""
    entries = await _claim_due(pending_key=HELD_KEY, entry_prefix=HELD_ENTRY_PREFIX)
    if not entries:
        return 0
    from app.core.database import async_session

    started = 0
    for entry in entries:
        try:
            async with async_session() as db:
                run_id = await _evaluate_held(db, entry)
                await db.commit()
            if run_id is not None:
                started += 1
        except Exception as exc:
            logger.error(
                "Lease hold of assignment %s failed: %s",
                entry.get("assignment_id"),
                format_exception_message(exc),
                exc_info=True,
            )
    return started


async def _evaluate_watch(db: Any, entry: dict[str, Any], *, honour_admitted: bool = True) -> str | None:
    """Read a watched head again: trigger the assignment when its filter now admits it; the run id.

    *honour_admitted* ``False`` is a held event (:func:`hold_for_lease`): its
    delivery WAS admitted, its run stood down, so the admission does not end it.
    """
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.services import event_enrichment, integration_event_filters
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError, owner_not_deactivated

    assignment_id = safe_uuid(entry.get("assignment_id"))
    provider = entry.get("provider")
    event_key = entry.get("event_key")
    payload = entry.get("payload")
    subject = entry.get("subject")
    if assignment_id is None or not isinstance(provider, str) or not isinstance(event_key, str):
        return None
    if not isinstance(payload, dict) or not isinstance(subject, str):
        return None
    if honour_admitted and await is_admitted(assignment_id, subject):
        return None  # the real last delivery came and was admitted
    wf = (
        await db.execute(
            select(Assignment).where(
                Assignment.id == assignment_id,
                Assignment.enabled.is_(True),
                owner_not_deactivated(),
            )
        )
    ).scalar_one_or_none()
    if wf is None or wf.trigger_type != "integration_event":
        return None
    config = wf.trigger_config if isinstance(wf.trigger_config, dict) else {}
    if config.get("provider") != provider:
        return None
    agent = await db.get(Agent, wf.agent_id)
    if agent is None or not agent.enabled:
        return None
    filter_config = integration_event_filters.trigger_filter(config)
    if integration_event_filters.plain_filter_reason(config.get("event") or "", payload, filter_config) is not None:
        return None  # the filter changed and drops the delivery on its own keys
    facts = await event_enrichment.enrichment_for(
        db,
        provider,
        event_key,
        payload,
        trigger_config=config,
        agent=agent,
        receiver_integration_id=entry.get("receiver_integration_id"),
        fresh=True,
    )
    reason = integration_event_filters.event_filter_reason(
        config.get("event") or "",
        event_enrichment.with_enrichment(payload, event_enrichment.as_watched_delivery(provider, facts)),
        filter_config,
    )
    if reason is not None:
        unreadable = event_enrichment.enrichment_retryable(facts)
        if honour_admitted and (unreadable or not event_enrichment.facts_settled(provider, facts)):
            # Still running (or not read this time): read again at the next interval.
            await watch(
                assignment_id=assignment_id,
                provider=provider,
                event_key=event_key,
                payload=payload,
                subject=subject,
                receiver_integration_id=entry.get("receiver_integration_id"),
                trigger_context=entry.get("trigger_context"),
            )
        return None

    from app.services import assignment_engine

    trigger_context = dict(entry.get("trigger_context") or {})
    trigger_context.setdefault("type", "webhook")
    trigger_context.setdefault("provider", provider)
    trigger_context.setdefault("event_key", event_key)
    trigger_context["body"] = payload
    if honour_admitted:
        trigger_context["head_watch"] = {"subject": subject, "watched_since": entry.get("watched_since")}
    await mark_admitted(assignment_id, subject)
    try:
        run_id = await assignment_engine.trigger_assignment(
            assignment=wf,
            agent=agent,
            payload=payload,
            db=db,
            trigger_context=trigger_context,
            skip_if_active=True,
        )
        if run_id is None:
            run_id = await assignment_engine.queue_busy_pushed_event(
                wf, agent, payload, db=db, trigger_context=trigger_context
            )
    except AssignmentOwnerDeactivatedError:
        return None
    logger.warning(
        "Head watch: %s settled with no admitted delivery for assignment %s (a lost delivery): run %s",
        subject,
        assignment_id,
        run_id,
    )
    return run_id


async def process_watched_heads() -> int:
    """Read the due watched heads again; the number of runs they started."""
    entries = await _claim_due(pending_key=WATCH_KEY, entry_prefix=WATCH_ENTRY_PREFIX)
    if not entries:
        return 0
    from app.core.database import async_session

    started = 0
    for entry in entries:
        try:
            async with async_session() as db:
                run_id = await _evaluate_watch(db, entry)
                await db.commit()
            if run_id is not None:
                started += 1
        except Exception as exc:
            logger.error(
                "Head watch of assignment %s failed: %s",
                entry.get("assignment_id"),
                format_exception_message(exc),
                exc_info=True,
            )
    return started


async def process_due_retries() -> int:
    """Evaluate the due deferred deliveries; the number of runs they started."""
    entries = await _claim_due()
    if not entries:
        return 0
    from app.core.database import async_session

    started = 0
    for entry in entries:
        try:
            async with async_session() as db:
                run_id = await _evaluate(db, entry)
                await db.commit()
            if run_id is not None:
                started += 1
        except Exception as exc:
            logger.error(
                "Enrichment retry of assignment %s failed: %s",
                entry.get("assignment_id"),
                format_exception_message(exc),
                exc_info=True,
            )
    return started
