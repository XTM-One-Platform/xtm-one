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

"""Durable orchestration engine — FSM + transactional outbox primitives.

This module is the single scheduling / leasing / retry / recovery contract for
every long-running multi-step platform job. It owns no business logic — adapters
(see ``orchestration_adapters/``) plug their behaviour in; the reconciler (see
``orchestration_reconciler.py``) drives the loop.

Transaction contract
---------------------
Every mutator here **flushes but never commits** — the caller owns the
transaction boundary (mirrors ``evaluation_continuation``'s helper style). The
reconciler and producers commit explicitly. ``create_execution`` is therefore
safe to call inside a producer's transaction (e.g. alongside the ``AssignmentRun``
insert) so the row and its job appear atomically.

Concurrency model
-----------------
- ``claim_due`` / ``claim_outbox_events`` use ``FOR UPDATE SKIP LOCKED`` so any
  number of worker replicas can run the reconciler concurrently and claim
  disjoint rows — horizontal scaling with no external lock.
- ``complete`` / ``fail`` / ``extend_lease`` / ``await_event`` re-read the row
  under ``FOR UPDATE`` and verify lease ownership before mutating, so a worker
  holding a stale in-memory row (the session is ``expire_on_commit=False``)
  cannot clobber a row another transaction has already reclaimed or dead-lettered.
- Time is the app clock (``datetime.now(UTC)``), matching the rest of the
  codebase; bound parameters keep every comparison on a single clock.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.utils import chunked, safe_uuid
from app.models.orchestration import (
    TERMINAL_EXECUTION_STATES,
    OrchestrationExecution,
    OrchestrationOutboxEvent,
)

logger = logging.getLogger(__name__)

# Redis pub/sub channel used to nudge the reconciler the instant a due-now
# execution or outbox event is created, so manual triggers feel instant. The
# reconciler also polls on a short interval, so a missed wake only costs latency.
ORCHESTRATION_WAKE_CHANNEL = "orchestration:wake"

# Retry backoff (operational, not security-sensitive).
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 600  # 10 min cap

# Where ``defer`` records a hold on the execution payload (see ``defer``).
HOLD_PAYLOAD_KEY = "hold"
# Where ``reclaim_expired_leases`` records the lease expiry it acted on, read
# by ``lease_expiry``.
LEASE_EXPIRY_PAYLOAD_KEY = "lease_expired"
# Where ``defer(place="keep")`` records when a deferred execution may be
# claimed again while it keeps its place (its ``run_at``) in the queue.
NOT_BEFORE_PAYLOAD_KEY = "not_before"
#: Where ``defer(claim_for=...)`` records the worker a deferred execution is handed to: ``{worker, until}``,
#: only that worker claims it until then, any worker after.
CLAIM_FOR_PAYLOAD_KEY = "claim_for"
#: Where ``defer(not_for=...)`` records the worker that cannot start a deferred execution yet:
#: ``{worker, until}``, that worker does not claim it until then, any other worker claims it at once.
NOT_FOR_PAYLOAD_KEY = "not_for"
#: Where ``defer(not_release=...)`` records the release whose workers do not claim a deferred execution yet:
#: ``{version, until}``, a worker running that version waits until then, a worker of any other release claims it
#: at once. What a worker stopped by a rolling update hands its work back with when no worker of the incoming
#: release runs yet: a worker of its own release is stopped by the same rollout next.
NOT_RELEASE_PAYLOAD_KEY = "not_release"
#: What a claim clears from the payload: the deferral's hold on who may claim it, and when.
_CLAIM_HOLDS = (NOT_BEFORE_PAYLOAD_KEY, CLAIM_FOR_PAYLOAD_KEY, NOT_FOR_PAYLOAD_KEY, NOT_RELEASE_PAYLOAD_KEY)
#: Where ``claim_due(slot=...)`` records the adapter whose idle slot a claimed execution runs in: that
#: adapter's name, the lane it holds a slot of while it runs. A claim into its own adapter's slots drops it.
LENT_SLOT_PAYLOAD_KEY = "lent_slot"
#: Where ``claim_due(keep_slot=True)`` records when an execution first took a slot: an execution keeps it to
#: its end, through a hand-over, before every execution that took one after it (``waiting_for_slot``).
SLOT_SINCE_PAYLOAD_KEY = "slot_since"
#: Where a creator records the execution budget of work whose deadline counts from its start, not from its
#: creation (:func:`queued_deadline`): the seconds (timeout + grace) the work may run once a worker first claims
#: it. Until then ``deadline_at`` is the queue-wait bound; :func:`claim_due` moves it to ``start + budget``.
START_BUDGET_PAYLOAD_KEY = "start_budget_seconds"
#: Where :func:`claim_due` records when such work was first claimed (ISO time): set once, kept by every re-claim.
STARTED_AT_PAYLOAD_KEY = "started_at"
#: Where :func:`claim_due` (at the start) or :func:`sweep_deadlines` (expired in the queue) records the seconds
#: the work waited for a worker.
QUEUE_WAIT_PAYLOAD_KEY = "queue_wait_seconds"
#: Set by :func:`sweep_deadlines` on work that reached its queue-wait bound without ever starting.
EXPIRED_BEFORE_START_PAYLOAD_KEY = "expired_before_start"
#: Where a deferral puts the execution in its adapter's queue: ``after``
#: what is due before ``now + delay`` (the default), ``keep`` its own place -
#: claimable again after the delay, still ahead of what came after it -, or
#: ``head``: first, due now (work a stopping worker handed back).
DEFER_PLACES = ("after", "keep", "head")
#: How far before the earliest pending execution of its adapter one put at the head is due.
_HEAD_STEP = timedelta(seconds=1)

# The authoritative transition table. ``running -> pending`` is the in-band
# retry path (and the ``defer`` hold, which gives the attempt back);
# ``failed | dead_letter -> pending`` is the operator requeue path
# (``requeue_execution``) — both are reachable and tested, so there are no
# dead transitions in this map.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled"},
    "running": {"completed", "failed", "dead_letter", "pending", "cancelled"},
    "failed": {"pending"},
    "dead_letter": {"pending"},
    "completed": set(),
    "cancelled": set(),
}


class OrchestrationError(Exception):
    """Base class for orchestration-engine errors."""


class InvalidTransitionError(OrchestrationError):
    """Raised when a state transition is not permitted by the transition table."""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Illegal orchestration transition {from_state!r} -> {to_state!r}"
        )
        self.from_state = from_state
        self.to_state = to_state


class LeaseLostError(OrchestrationError):
    """Raised when a caller no longer owns the lease it is trying to act under.

    The worker that catches this should bail cleanly — another transaction has
    reclaimed, dead-lettered, or completed the row.
    """


class ResumeNotReadyError(OrchestrationError):
    """Raised when a resume arrives before its target execution has paused.

    A ``resume`` outbox event can race ahead of the AWAIT pause: the execution
    is still ``running`` under an active worker lease. Re-arming it then would
    steal the live lease and cause a duplicate/overlapping run. The outbox
    machinery retries (backs off) the event until the worker releases the lease
    and the execution is genuinely paused.
    """


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with +/-25% jitter, capped at ``MAX_BACKOFF_SECONDS``.

    Jitter de-synchronises concurrent reconciler workers so they don't retry in
    lock-step.
    """
    raw = BASE_BACKOFF_SECONDS * (2 ** max(0, attempt - 1))
    capped = min(raw, MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0.75, 1.25)  # noqa: S311 - operational backoff, not crypto
    return min(capped * jitter, float(MAX_BACKOFF_SECONDS))


# The floor of the bound work is handed: never zero or negative, should the
# deadline run out between ``deadline_spent`` and the ``wait_for`` it bounds.
MIN_RUNNABLE_SECONDS = 1.0


def runnable_seconds_left(
    deadline_at: datetime | None, *, now: datetime | None = None
) -> float | None:
    """Seconds the work of an execution may still run before its deadline.

    ``deadline_at`` sits ``orchestration_grace_seconds`` past the end of the
    budget, the slack that lets the work's own bound fire before
    ``sweep_deadlines``: what is left is the time to the deadline minus that
    grace. ``None`` when the execution has no deadline.
    """
    if deadline_at is None:
        return None
    now = now or datetime.now(UTC)
    return (deadline_at - now).total_seconds() - settings.orchestration_grace_seconds


def bound_by_deadline(
    budget: float | None,
    deadline_at: datetime | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """The in-process bound of an execution's work: its budget, never past what
    its deadline leaves.

    A worker restart re-runs an execution from the start under the deadline it
    was created with. Re-armed with its whole budget, the work believed it had
    time it did not have: it never wound down, and ``sweep_deadlines``
    dead-lettered it mid-work with nothing recorded. On a first execution the
    two agree, to the claim latency.

    ``budget=None`` (no per-run bound) is bounded by the deadline alone.
    """
    left = runnable_seconds_left(deadline_at, now=now)
    if left is None:
        return budget
    left = max(MIN_RUNNABLE_SECONDS, left)
    if budget is None:
        return left
    return min(float(budget), left)


def deadline_spent(deadline_at: datetime | None, *, now: datetime | None = None) -> bool:
    """The deadline leaves the work no runnable time at all.

    A row is claimable until ``deadline_at``, so a re-run reclaimed inside the
    grace window, or work picked up that late, would start with nothing left:
    it records its own timeout before any setup and before anything is
    charged, instead of racing the sweep through a setup it cannot use. Asked
    again once the setup is done, so a setup that used what was left never
    starts a loop that can only time out.
    """
    left = runnable_seconds_left(deadline_at, now=now)
    return left is not None and left <= 0


def queued_deadline(
    run_at: datetime | None,
    budget_seconds: float | None,
    *,
    max_queue_wait_seconds: float | None = None,
) -> tuple[datetime | None, dict]:
    """The ``deadline_at`` and payload entry of work whose deadline counts from its start.

    *budget_seconds* is what the work may run once a worker starts it (its timeout plus
    ``orchestration_grace_seconds``); ``None`` sets no deadline at all (timeout disabled). Until
    the first claim the deadline is the queue-wait bound, ``run_at`` plus *max_queue_wait_seconds*
    (``settings.orchestration_max_queue_wait_seconds`` when ``None``; ``0`` or less: no queue bound);
    :func:`claim_due` then sets it to the start plus the budget it finds under
    :data:`START_BUDGET_PAYLOAD_KEY`. Time spent waiting for a worker is never charged to the work.
    """
    if budget_seconds is None:
        return None, {}
    wait = settings.orchestration_max_queue_wait_seconds if max_queue_wait_seconds is None else max_queue_wait_seconds
    due = run_at or datetime.now(UTC)
    deadline = due + timedelta(seconds=float(wait)) if wait and wait > 0 else None
    return deadline, {START_BUDGET_PAYLOAD_KEY: float(budget_seconds)}


def started_at(execution: OrchestrationExecution) -> datetime | None:
    """When a worker first claimed *execution* (:data:`STARTED_AT_PAYLOAD_KEY`); ``None`` before."""
    return _moment((getattr(execution, "payload", None) or {}).get(STARTED_AT_PAYLOAD_KEY))


def expired_before_start(execution: OrchestrationExecution) -> bool:
    """*execution* reached its queue-wait bound without ever being started by a worker."""
    return bool((getattr(execution, "payload", None) or {}).get(EXPIRED_BEFORE_START_PAYLOAD_KEY))


def queue_wait_seconds(execution: OrchestrationExecution) -> float | None:
    """The seconds *execution* waited for a worker, once known (started, or expired in the queue)."""
    value = (getattr(execution, "payload", None) or {}).get(QUEUE_WAIT_PAYLOAD_KEY)
    return float(value) if isinstance(value, (int, float)) else None


def expired_before_start_message(work: str, execution: OrchestrationExecution) -> str:
    """What work that waited past its queue-wait bound without starting says."""
    waited = queue_wait_seconds(execution)
    after = f" after {int(waited)} s" if waited is not None else ""
    return (
        f"{work} never started: no worker was free to run it{after} in the queue "
        "(deadline_exceeded_before_start). Nothing ran and nothing was charged."
    )


def deadline_spent_message(work: str, deadline_at: datetime | None) -> str:
    """What work that :func:`deadline_spent` stopped before its loop ran says."""
    when = f" ({deadline_at.isoformat()})" if deadline_at is not None else ""
    return (
        f"{work} timed out before it could run: its deadline{when} left none "
        "of its time budget."
    )


def deadline_cut_budget(budget: float | None, bound: float | None) -> bool:
    """The deadline cut the work's *bound* short of its *budget* by more than
    ``orchestration_grace_seconds``.

    The work lost more than the engine's slack before it started (a backlog, an
    attempt the engine lost): given its whole budget it would have outrun its
    deadline, and ``sweep_deadlines`` would have dead-lettered it and credited
    its action back free. A timeout on such a bound is the engine's, not the
    work's, so it is credited back free too, never from the agent's failure
    pool. Within the grace, the bound is the budget less the claim latency and
    its timeout is the work's own, as it always was.
    """
    return (
        budget is not None
        and bound is not None
        and float(budget) - float(bound) > settings.orchestration_grace_seconds
    )


def _check_transition(from_state: str, to_state: str) -> None:
    if to_state not in _ALLOWED_TRANSITIONS.get(from_state, set()):
        raise InvalidTransitionError(from_state, to_state)


def _require_owner(
    execution: OrchestrationExecution, worker_id: str | None, require_lease: bool
) -> None:
    """Verify the caller still owns the lease (after a locked re-read).

    Skipped when ``require_lease`` is False — the outbox-driven path
    (``handle_event``) is serialised by the outbox row lock instead of a lease.
    """
    if not require_lease:
        return
    if worker_id is None or execution.lease_owner != worker_id:
        logger.debug(
            "lease race: execution %s owned by %r but caller is %r — lease lost",
            execution.id,
            execution.lease_owner,
            worker_id,
        )
        raise LeaseLostError(f"Lease for execution {execution.id} lost")


async def _locked_get(
    db: AsyncSession, execution_id: uuid.UUID, *, refresh: bool = False
) -> OrchestrationExecution | None:
    """``SELECT ... FOR UPDATE`` the execution row.

    ``refresh=True`` adds ``populate_existing`` so the row's CURRENT database
    values overwrite whatever the session already has in its identity map. That
    matters whenever the caller's session loaded the row EARLIER and is now
    re-reading it to decide something about its live state: sessions are created
    with ``expire_on_commit=False``, so the ORM's default "do not overwrite
    already-loaded attributes" rule would hand back the stale instance and the
    lock would guard a decision made on pre-lock values. Every transition whose
    caller may already hold the row uses it: the reconciler applies
    ``complete`` / ``fail`` / ``await_event`` on the session that loaded the row
    before the adapter ran, an outbox handler calls ``rearm_for_resume`` on the
    session that read the row to route the event, and ``cancel_execution``'s
    callers may look the execution up before cancelling it. Callers that lock
    a row they have not already loaded do not need it and pay one less
    attribute refresh.
    """
    stmt = (
        select(OrchestrationExecution)
        .where(OrchestrationExecution.id == execution_id)
        .with_for_update()
    )
    if refresh:
        stmt = stmt.execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _locked_get_outbox(
    db: AsyncSession, event_id: uuid.UUID
) -> OrchestrationOutboxEvent | None:
    return (
        await db.execute(
            select(OrchestrationOutboxEvent)
            .where(OrchestrationOutboxEvent.id == event_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


# ── Lifecycle: create / claim / lease ────────────────────────────────────────


async def create_execution(
    db: AsyncSession,
    *,
    adapter: str,
    payload: dict,
    run_at: datetime | None = None,
    deadline_at: datetime | None = None,
    dedup_key: str | None = None,
    max_attempts: int | None = None,
) -> OrchestrationExecution:
    """Insert a new ``pending`` execution.

    Idempotent on ``dedup_key`` via ``INSERT ... ON CONFLICT DO NOTHING`` — a
    re-trigger with the same key returns the existing row instead of creating a
    duplicate. Flushes (does not commit). Call ``notify_orchestration_wake()``
    after the surrounding transaction commits for near-instant dispatch.
    """
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "adapter": adapter,
        "state": "pending",
        "payload": payload or {},
        "run_at": run_at or now,
        "deadline_at": deadline_at,
        "attempt_count": 0,
        "max_attempts": max_attempts or settings.orchestration_default_max_attempts,
        "dedup_key": dedup_key,
        "created_at": now,
        "updated_at": now,
    }

    if dedup_key is None:
        execution = OrchestrationExecution(**values)
        db.add(execution)
        await db.flush()
        return execution

    stmt = (
        pg_insert(OrchestrationExecution)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_orchestration_executions_dedup_key")
        .returning(OrchestrationExecution.id)
    )
    inserted_id = (await db.execute(stmt)).scalar_one_or_none()
    if inserted_id is None:
        # Conflict — return the existing row (the canonical idempotent result).
        return (
            await db.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.dedup_key == dedup_key
                )
            )
        ).scalar_one()
    return (
        await db.execute(
            select(OrchestrationExecution).where(
                OrchestrationExecution.id == inserted_id
            )
        )
    ).scalar_one()


#: The most due executions an adapter's ``deal`` hook orders in one claim (:func:`claim_due`).
DEAL_WINDOW = 500

#: ``deal(db, due, worker_id)``: the *due* executions of an adapter in the order its claim takes them (:func:`claim_due`).
ClaimDeal = Callable[[AsyncSession, list[OrchestrationExecution], str], Awaitable[list[OrchestrationExecution]]]


def _claimable(adapter: str, worker_id: str, now: datetime) -> tuple:
    """The conditions a ``pending`` execution of *adapter* meets when *worker_id* may claim it now (:func:`claim_due`)."""
    payload = OrchestrationExecution.payload
    return (
        OrchestrationExecution.adapter == adapter,
        OrchestrationExecution.state == "pending",
        OrchestrationExecution.run_at <= now,
        or_(
            OrchestrationExecution.lease_owner.is_(None),
            OrchestrationExecution.lease_expires_at < now,
        ),
        OrchestrationExecution.attempt_count
        < OrchestrationExecution.max_attempts,
        or_(
            OrchestrationExecution.deadline_at.is_(None),
            OrchestrationExecution.deadline_at > now,
        ),
        or_(
            ~payload.has_key(NOT_BEFORE_PAYLOAD_KEY),
            cast(payload[NOT_BEFORE_PAYLOAD_KEY].astext, DateTime(timezone=True)) <= now,
        ),
        or_(
            ~payload.has_key(CLAIM_FOR_PAYLOAD_KEY),
            payload[(CLAIM_FOR_PAYLOAD_KEY, "worker")].astext == worker_id,
            cast(payload[(CLAIM_FOR_PAYLOAD_KEY, "until")].astext, DateTime(timezone=True)) <= now,
        ),
        or_(
            ~payload.has_key(NOT_FOR_PAYLOAD_KEY),
            payload[(NOT_FOR_PAYLOAD_KEY, "worker")].astext != worker_id,
            cast(payload[(NOT_FOR_PAYLOAD_KEY, "until")].astext, DateTime(timezone=True)) <= now,
        ),
        or_(
            ~payload.has_key(NOT_RELEASE_PAYLOAD_KEY),
            payload[(NOT_RELEASE_PAYLOAD_KEY, "version")].astext != str(settings.app_version or ""),
            cast(payload[(NOT_RELEASE_PAYLOAD_KEY, "until")].astext, DateTime(timezone=True)) <= now,
        ),
    )


async def claim_due(
    db: AsyncSession,
    *,
    adapter: str,
    worker_id: str,
    limit: int,
    lease_seconds: int | None = None,
    deal: ClaimDeal | None = None,
    slot: str | None = None,
    keep_slot: bool = False,
    ripe_seconds: float = 0.0,
) -> list[OrchestrationExecution]:
    """Atomically claim up to ``limit`` due ``pending`` executions for ``adapter``.

    Eligibility: ``pending`` + ``run_at <= now`` + no valid lease +
    ``attempt_count < max_attempts`` + not past ``deadline_at`` + past the
    ``not_before`` of a deferral that kept its place + handed to
    *worker_id* or to no worker, or past the hand's ``until`` + not
    held from *worker_id* (``not_for``), or past that hold. Claimed oldest
    ``run_at`` first: a job's place in the queue - or, with *deal*, in
    the order the adapter deals its due executions (up to :data:`DEAL_WINDOW`
    of them, oldest ``run_at`` first), each locked in turn and one taken by
    another worker meanwhile skipped: an adapter whose queue is not first come,
    first served (the Video Studio preview lane, one preview per video in turn)
    is claimed in its own order at the moment of the claim. Claimed rows are
    flipped to ``running`` with a lease and ``attempt_count`` incremented (so a
    crashed worker eventually exhausts attempts and dead-letters rather than
    spinning). ``FOR UPDATE SKIP LOCKED`` makes this safe across worker replicas.
    *slot* names the adapter whose idle slots they are claimed into
    (:data:`LENT_SLOT_PAYLOAD_KEY`): what counts the holders of that lane reads.
    With *keep_slot*, each records when it first took a slot
    (:data:`SLOT_SINCE_PAYLOAD_KEY`), kept through its hand-overs. With *ripe_seconds*, only executions due
    that long at least: what a worker leaves to the others for that long first.
    """
    now = datetime.now(UTC)
    lease_seconds = lease_seconds or settings.orchestration_lease_seconds
    eligible = _claimable(adapter, worker_id, now)
    if ripe_seconds > 0:
        eligible = (*eligible, OrchestrationExecution.run_at <= now - timedelta(seconds=ripe_seconds))
    if deal is None:
        stmt = (
            select(OrchestrationExecution)
            .where(*eligible)
            .order_by(OrchestrationExecution.run_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await db.execute(stmt)).scalars().all())
    else:
        due = (
            await db.execute(
                select(OrchestrationExecution).where(*eligible).order_by(OrchestrationExecution.run_at).limit(DEAL_WINDOW)
            )
        ).scalars().all()
        rows = []
        for candidate in await deal(db, list(due), worker_id) if due else []:
            if len(rows) >= limit:
                break
            locked = (
                await db.execute(
                    select(OrchestrationExecution)
                    .where(OrchestrationExecution.id == candidate.id, *eligible)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if locked is not None:
                rows.append(locked)
    lease_expires = now + timedelta(seconds=lease_seconds)
    for row in rows:
        row.state = "running"
        row.lease_owner = worker_id
        row.lease_expires_at = lease_expires
        row.attempt_count += 1
        row.updated_at = now
        payload = {k: v for k, v in (row.payload or {}).items() if k not in _CLAIM_HOLDS and k != LENT_SLOT_PAYLOAD_KEY}
        if slot is not None:
            payload[LENT_SLOT_PAYLOAD_KEY] = slot
        if keep_slot and SLOT_SINCE_PAYLOAD_KEY not in payload:
            payload[SLOT_SINCE_PAYLOAD_KEY] = now.isoformat()
        _start_execution_clock(row, payload, now)
        if payload != (row.payload or {}):
            row.payload = payload
    await db.flush()
    return rows


def _start_execution_clock(row: OrchestrationExecution, payload: dict, now: datetime) -> None:
    """At the first claim of work created by :func:`queued_deadline`, its deadline becomes start + budget.

    The queue wait is recorded beside it. A re-claim (a hand-back, a lost worker, a deferral) keeps
    the start and the deadline it set: only the first start moves the clock.
    """
    budget = payload.get(START_BUDGET_PAYLOAD_KEY)
    if not isinstance(budget, (int, float)) or payload.get(STARTED_AT_PAYLOAD_KEY):
        return
    payload[STARTED_AT_PAYLOAD_KEY] = now.isoformat()
    due = _aware(row.run_at) if row.run_at is not None else now
    payload[QUEUE_WAIT_PAYLOAD_KEY] = round(max(0.0, (now - due).total_seconds()), 3)
    row.deadline_at = now + timedelta(seconds=float(budget))


def _moment(recorded: object) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(recorded))) if recorded else None
    except (TypeError, ValueError):
        return None


def slot_since(execution: OrchestrationExecution) -> datetime | None:
    """When *execution* first took a slot (:data:`SLOT_SINCE_PAYLOAD_KEY`); ``None`` before it did."""
    return _moment((getattr(execution, "payload", None) or {}).get(SLOT_SINCE_PAYLOAD_KEY))


async def waiting_for_slot(
    db: AsyncSession,
    *,
    adapter: str,
    worker_id: str,
    states: Iterable[str],
    waited_seconds: float = 0.0,
) -> list[OrchestrationExecution]:
    """The executions of *adapter* waiting for the slot they held, oldest slot first: due, *worker_id* may
    claim them, no worker has them in hand (``claim_for``), each took a slot before (:func:`slot_since`) and lost it
    in one of *states* - handed back by a stopping worker, taken back from a worker gone - *waited_seconds* ago at
    least."""
    now = datetime.now(UTC)
    payload = OrchestrationExecution.payload
    rows = (
        await db.execute(
            select(OrchestrationExecution)
            .where(
                *_claimable(adapter, worker_id, now),
                OrchestrationExecution.adapter_state.in_(list(states)),
                payload.has_key(SLOT_SINCE_PAYLOAD_KEY),
                or_(
                    ~payload.has_key(CLAIM_FOR_PAYLOAD_KEY),
                    cast(payload[(CLAIM_FOR_PAYLOAD_KEY, "until")].astext, DateTime(timezone=True)) <= now,
                ),
                OrchestrationExecution.updated_at <= now - timedelta(seconds=max(0.0, waited_seconds)),
            )
            .order_by(OrchestrationExecution.run_at)
            .limit(DEAL_WINDOW)
        )
    ).scalars().all()
    held = [(since, row) for row in rows if (since := slot_since(row)) is not None]
    return [row for _since, row in sorted(held, key=lambda item: (item[0], str(item[1].id)))]


async def hand_to(db: AsyncSession, *, execution_id: uuid.UUID, worker_id: str, seconds: float) -> bool:
    """Hand the pending *execution_id* to *worker_id* for *seconds* (``claim_for``): whether it was, no other
    worker having it in hand already."""
    now = datetime.now(UTC)
    execution = (
        await db.execute(
            select(OrchestrationExecution)
            .where(OrchestrationExecution.id == execution_id, OrchestrationExecution.state == "pending")
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if execution is None:
        return False
    hand = (execution.payload or {}).get(CLAIM_FOR_PAYLOAD_KEY)
    if isinstance(hand, dict) and hand.get("worker") != worker_id and (_moment(hand.get("until")) or now) > now:
        return False
    until = now + timedelta(seconds=max(0.0, float(seconds)))
    execution.payload = {**(execution.payload or {}), CLAIM_FOR_PAYLOAD_KEY: {"worker": str(worker_id), "until": until.isoformat()}}
    await db.flush()
    return True


async def running_on(db: AsyncSession, *, adapter: str, owners: Iterable[str]) -> list[uuid.UUID]:
    """The ``running`` executions of *adapter* whose live lease one of *owners* holds, oldest slot first."""
    wanted = [owner for owner in owners if owner]
    if not wanted:
        return []
    rows = (
        await db.execute(
            select(OrchestrationExecution.id, OrchestrationExecution.payload).where(
                OrchestrationExecution.adapter == adapter,
                OrchestrationExecution.state == "running",
                OrchestrationExecution.lease_owner.in_(wanted),
                OrchestrationExecution.lease_expires_at >= datetime.now(UTC),
            )
        )
    ).all()
    far = datetime.max.replace(tzinfo=UTC)
    ordered = sorted(rows, key=lambda row: (_moment((row.payload or {}).get(SLOT_SINCE_PAYLOAD_KEY)) or far, str(row.id)))
    return [row.id for row in ordered]


async def extend_lease(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int | None = None,
) -> datetime | None:
    """Heartbeat the lease mid-work; the execution's deadline. Raises ``LeaseLostError`` if stolen/gone."""
    now = datetime.now(UTC)
    lease_seconds = lease_seconds or settings.orchestration_lease_seconds
    execution = await _locked_get(db, execution_id)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease=True)
    if execution.state != "running":
        # Terminal-state race: a sweep/cancel beat the heartbeat.
        logger.debug(
            "terminal race: extend_lease on %s in state %s",
            execution_id,
            execution.state,
        )
        raise LeaseLostError(f"Execution {execution_id} is {execution.state}")
    execution.lease_expires_at = now + timedelta(seconds=lease_seconds)
    execution.updated_at = now
    await db.flush()
    return execution.deadline_at


def superseded_by(
    *,
    state: str | None,
    lease_owner: str | None,
    attempt_count: int | None,
    worker_id: str,
    attempt: int | None,
) -> str | None:
    """Why *worker_id*, running *attempt* of an execution now in *state* under *lease_owner* at *attempt_count*, is
    no longer its single writer, or ``None`` when it still is.

    Taken over: ``running`` under another worker's lease or at a later attempt, or ``pending`` after a take-back
    (queued for the next claim). Gone: the row no longer exists. A terminal row is not a take-over: the cancel
    watcher and the turn's own end handle it as before.
    """
    if state is None:
        return "its execution no longer exists"
    if state == "pending":
        return "its execution was taken back and is queued for another worker"
    if state != "running":
        return None
    if lease_owner is not None and lease_owner != worker_id:
        return f"its execution runs on worker {lease_owner}"
    if attempt is not None and attempt_count is not None and attempt_count > attempt:
        return f"attempt {attempt_count} of its execution replaced attempt {attempt}"
    return None


async def extend_deadline(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    deadline_at: datetime,
) -> datetime | None:
    """Push a running execution's deadline out to *deadline_at* - never in - as its lease owner; the deadline now.

    For work whose length is only known once it runs: a Video Studio render drawing its scene ranges one after
    another keeps a deadline per range. The reconciler's timeout of the running task follows the row at
    its next heartbeat (``orchestration_reconciler._follow_deadline``). ``LeaseLostError`` once the execution is not
    this worker's. Flushes; the caller commits.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease=True)
    if execution.state != "running":
        raise LeaseLostError(f"Execution {execution_id} is {execution.state}")
    if execution.deadline_at is not None and execution.deadline_at < deadline_at:
        execution.deadline_at = deadline_at
        execution.updated_at = now
        await db.flush()
    return execution.deadline_at


#: Where a running execution whose deadline was given time back keeps the deadline it had before the first push:
#: what :func:`give_deadline_back` bounds every later push by.
DEADLINE_FIRST_KEY = "deadline_first_at"


async def give_deadline_back(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    seconds: float | None = None,
    until: datetime | None = None,
    most_seconds: float,
) -> tuple[datetime | None, float]:
    """Give a running execution time back, as its lease owner: its deadline moved out by *seconds*, or out to *until*.

    For time the work did not spend on itself (a worker stopped under it, a relaunch) or that its measured pace says
    it needs. Never in, and never past *most_seconds* after the deadline it had before the first time it was given
    some back (kept in its payload, :data:`DEADLINE_FIRST_KEY`): an execution handed on forever never runs forever.
    Answers ``(the deadline now, the seconds given back by this call)``. ``LeaseLostError`` once the execution is
    not this worker's. Flushes; the caller commits.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease=True)
    if execution.state != "running":
        raise LeaseLostError(f"Execution {execution_id} is {execution.state}")
    current = execution.deadline_at
    if current is None:
        return None, 0.0
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    payload = dict(execution.payload or {})
    first = _parse_instant(payload.get(DEADLINE_FIRST_KEY)) or current
    target = until if until is not None else current + timedelta(seconds=max(0.0, float(seconds or 0.0)))
    target = min(target, first + timedelta(seconds=max(0.0, most_seconds)))
    if target <= current:
        return current, 0.0
    payload.setdefault(DEADLINE_FIRST_KEY, first.isoformat())
    execution.payload = payload
    execution.deadline_at = target
    execution.updated_at = now
    await db.flush()
    return target, (target - current).total_seconds()


def _parse_instant(value: object) -> datetime | None:
    """*value*, an ISO time, in UTC; ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def checkpoint_adapter_state(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    adapter_state: str,
) -> None:
    """Persist ``adapter_state`` mid-work without changing lease or lifecycle.

    Lets a long, multi-cycle ``execute`` (an autonomous "continue" burst that
    runs several cycles under one lease before parking) checkpoint its progress
    cursor after each cycle, so a worker crash mid-burst resumes from the last
    COMPLETED cycle instead of rewinding to the previous park and re-running the
    whole burst. Mirrors :func:`extend_lease`: owner-checked, running-only,
    ``flush`` only - the CALLER must commit for the checkpoint to survive a
    crash. Raises :class:`LeaseLostError` if the lease was stolen or the row went
    terminal, so the caller can stop authoring against a run it no longer owns.

    Like the result transitions, this runs inside a session that loaded the
    execution before the work started - here REPEATEDLY - so it locks with
    ``refresh``: without ``populate_existing`` the ORM would return the
    already-loaded instance untouched (sessions use ``expire_on_commit=False``)
    and the ownership check would pass on pre-lock values - silently defeating
    the very guarantee this function offers its caller.
    """
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease=True)
    if execution.state != "running":
        raise LeaseLostError(f"Execution {execution_id} is {execution.state}")
    execution.adapter_state = adapter_state
    execution.updated_at = datetime.now(UTC)
    await db.flush()


# ── Lifecycle: terminal transitions ──────────────────────────────────────────


async def complete(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str | None = None,
    require_lease: bool = True,
    payload_update: dict | None = None,
    adapter_state: str | None = None,
) -> OrchestrationExecution:
    """Mark an execution ``completed`` and clear its lease."""
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease)
    _check_transition(execution.state, "completed")
    if payload_update:
        execution.payload = {**(execution.payload or {}), **payload_update}
    if adapter_state is not None:
        execution.adapter_state = adapter_state
    _clear_hold(execution)
    execution.state = "completed"
    execution.last_error = None
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.updated_at = now
    await db.flush()
    return execution


async def fail(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str | None = None,
    require_lease: bool = True,
    error: str,
    retryable: bool = True,
    backoff_seconds: float | None = None,
) -> OrchestrationExecution:
    """Record a failure: retry (-> ``pending``), give up (-> ``failed``), or
    exhaust (-> ``dead_letter``).

    Routing:
      * ``retryable=False``                       -> ``failed`` (definitive).
      * retryable, attempts left, before deadline -> ``pending`` (backoff retry).
      * retryable, exhausted or past deadline      -> ``dead_letter``.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease)

    attempts_left = execution.attempt_count < execution.max_attempts
    deadline_ok = execution.deadline_at is None or now < execution.deadline_at

    if not retryable:
        target = "failed"
    elif attempts_left and deadline_ok:
        target = "pending"
    else:
        target = "dead_letter"

    _check_transition(execution.state, target)
    execution.last_error = error[:2000] if error else None
    execution.lease_owner = None
    execution.lease_expires_at = None

    if target == "pending":
        backoff = (
            backoff_seconds
            if backoff_seconds is not None
            else _backoff_seconds(execution.attempt_count)
        )
        execution.run_at = now + timedelta(seconds=backoff)
        execution.adapter_state = None
    else:
        # A retry keeps a hold's start (the bound keeps counting through a
        # transient read failure); a final verdict ends it.
        _clear_hold(execution)

    execution.state = target
    execution.updated_at = now
    await db.flush()
    return execution


async def await_event(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str | None = None,
    require_lease: bool = True,
    adapter_state: str,
    deadline_at: datetime | None = None,
) -> OrchestrationExecution:
    """Pause a ``running`` execution while it waits for an external event.

    Keeps ``state='running'`` but releases the lease so the resume outbox event
    can drive it without fighting a heartbeat. ``claim_due`` ignores ``running``
    rows, ``reclaim_expired_leases`` ignores lease-less rows, so the only things
    that act on a paused execution are an inbound outbox event (resume) or the
    deadline sweep (timeout). Set ``deadline_at`` to bound the wait.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease)
    if execution.state != "running":
        raise InvalidTransitionError(execution.state, "running(await)")
    execution.adapter_state = adapter_state
    # Always assign the await deadline — including ``None``. An AWAIT with no
    # deadline means "wait indefinitely" (e.g. assignment HITL waiting on a
    # human), so we must be able to CLEAR a deadline that was set at creation.
    # Guarding this on ``is not None`` would leave the original deadline in
    # place and ``sweep_deadlines`` would dead-letter a HITL-paused execution
    # after the original timeout.
    execution.deadline_at = deadline_at
    _clear_hold(execution)
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.updated_at = now
    await db.flush()
    return execution


def hold_record(execution: OrchestrationExecution) -> dict | None:
    """The execution's current hold (``since`` ISO timestamp, ``count`` of
    deferrals, ``reason``), or ``None`` when it is not held."""
    record = (execution.payload or {}).get(HOLD_PAYLOAD_KEY)
    return record if isinstance(record, dict) else None


def hold_started_at(execution: OrchestrationExecution) -> datetime | None:
    record = hold_record(execution)
    if not record:
        return None
    try:
        since = datetime.fromisoformat(str(record.get("since")))
    except (TypeError, ValueError):
        return None
    return since if since.tzinfo else since.replace(tzinfo=UTC)


def _clear_hold(execution: OrchestrationExecution) -> None:
    # Only when there is one: an execution that was never held keeps its
    # payload exactly as it was.
    if hold_record(execution) is not None:
        payload = dict(execution.payload or {})
        payload.pop(HOLD_PAYLOAD_KEY, None)
        execution.payload = payload


async def count_waiting(
    db: AsyncSession,
    *,
    adapter: str,
    adapter_states: tuple[str, ...],
    waited_seconds: float,
    now: datetime | None = None,
) -> int:
    """How many pending executions of *adapter* in one of *adapter_states* are due and have waited *waited_seconds*
    since they went back to the queue: the jobs a lane takes its lent slots back for."""
    now = now or datetime.now(UTC)
    found = await db.execute(
        select(func.count())
        .select_from(OrchestrationExecution)
        .where(
            OrchestrationExecution.adapter == adapter,
            OrchestrationExecution.state == "pending",
            OrchestrationExecution.adapter_state.in_(adapter_states),
            OrchestrationExecution.run_at <= now,
            OrchestrationExecution.updated_at <= now - timedelta(seconds=max(0.0, float(waited_seconds))),
        )
    )
    return int(found.scalar_one() or 0)


async def head_of_queue(
    db: AsyncSession, adapter: str, *, now: datetime | None = None, excluding: uuid.UUID | None = None
) -> datetime:
    """The ``run_at`` that puts an execution first in *adapter*'s queue: before every pending one, never after now."""
    now = now or datetime.now(UTC)
    earliest = select(func.min(OrchestrationExecution.run_at)).where(
        OrchestrationExecution.adapter == adapter, OrchestrationExecution.state == "pending"
    )
    if excluding is not None:
        earliest = earliest.where(OrchestrationExecution.id != excluding)
    first = (await db.execute(earliest)).scalar_one_or_none()
    return min(now, _aware(first) - _HEAD_STEP) if first is not None else now


async def defer(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str | None = None,
    require_lease: bool = True,
    reason: str,
    delay_seconds: float,
    adapter_state: str,
    payload_update: dict | None = None,
    place: str = "after",
    claim_for: tuple[str, float] | None = None,
    not_for: str | None = None,
    not_release: tuple[str, float] | None = None,
) -> OrchestrationExecution:
    """Hand a claimed execution back to wait: the work could not start for a
    reason outside it (agentic AI unavailable), and will be due again in
    ``delay_seconds``.

    *place* (:data:`DEFER_PLACES`) is where it waits in its adapter's
    queue: ``after`` what is due before ``now + delay_seconds``; ``keep`` -
    its ``run_at`` untouched, claimable after the delay (``not_before``) -, so
    a job waiting its turn never falls behind the jobs queued after it; or
    ``head``: due now, before every pending execution of its adapter.
    *claim_for* ``(worker, seconds)`` hands it to that worker: only it
    claims the execution for that long (:data:`CLAIM_FOR_PAYLOAD_KEY`), any
    worker after - one that went meanwhile never strands it. *not_for* names
    the worker that cannot start it yet: with ``keep``, only that
    worker waits the delay (:data:`NOT_FOR_PAYLOAD_KEY`), any other claims it
    at once in its place - work waiting for one worker's room never hides from
    a worker that has it. *not_release* ``(version, seconds)``: no worker
    running that release claims it for that long after it is due
    (:data:`NOT_RELEASE_PAYLOAD_KEY`), a worker of any other release at once.

    Unlike a retryable :func:`fail`, a deferral is not an attempt, and neither
    is anything spent waiting. The first deferral records the attempts and the
    runnable time the work had left (``payload['hold']``'s ``attempts`` and
    ``budget_seconds``), and every deferral restores them: the claim is given
    back, and so is an attempt used meanwhile by a transient read retried
    through :func:`fail` or a worker lost during a re-check - consecutive
    ones still dead-letter; the deadline is set that far past the new
    ``run_at``, so the reconciler's claim lag is never charged and a held run
    that starts later keeps its whole timeout. A deferral on the outbox path
    (``require_lease=False``) follows no claim and gives none back. The
    adapter bounds how long it holds; ``adapter_state`` and ``last_error``
    say why it is waiting, and ``payload['hold']`` records since when and how
    many times (cleared by the next claim that runs, fails for good or
    pauses).

    Locks with ``refresh``: the reconciler defers on the session that loaded
    the row before the adapter ran (``expire_on_commit=False``), and a
    deferral decided on that copy would hand a claim another reconciler took
    after the lease lapsed back to ``pending``.
    """
    if place not in DEFER_PLACES:
        raise ValueError(f"defer place {place!r} is not one of {DEFER_PLACES}")
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease)
    _check_transition(execution.state, "pending")
    delay = timedelta(seconds=0.0 if place == "head" else max(0.0, float(delay_seconds)))
    previous = hold_record(execution) or {}
    attempts = previous.get("attempts")
    if not isinstance(attempts, int):
        attempts = max(0, execution.attempt_count - (1 if require_lease else 0))
    budget = previous.get("budget_seconds")
    if not isinstance(budget, (int, float)) and execution.deadline_at is not None:
        budget = max(0.0, (execution.deadline_at - now).total_seconds())
    record = {
        "since": previous.get("since") or now.isoformat(),
        "count": int(previous.get("count") or 0) + 1,
        "reason": adapter_state,
        "attempts": attempts,
    }
    if execution.deadline_at is not None:
        record["budget_seconds"] = budget
    payload = {**(execution.payload or {}), **(payload_update or {})}
    payload[HOLD_PAYLOAD_KEY] = record
    for hold in _CLAIM_HOLDS:
        payload.pop(hold, None)
    due = now + delay
    if claim_for is not None:
        worker, seconds = claim_for
        payload[CLAIM_FOR_PAYLOAD_KEY] = {"worker": str(worker), "until": (due + timedelta(seconds=max(0.0, float(seconds)))).isoformat()}
    if not_release is not None and not_release[0]:
        version, seconds = not_release
        payload[NOT_RELEASE_PAYLOAD_KEY] = {
            "version": str(version),
            "until": (due + timedelta(seconds=max(0.0, float(seconds)))).isoformat(),
        }
    if place == "keep" and not_for is not None:
        payload[NOT_FOR_PAYLOAD_KEY] = {"worker": str(not_for), "until": due.isoformat()}
    elif place == "keep":
        payload[NOT_BEFORE_PAYLOAD_KEY] = due.isoformat()
    elif place == "head":
        execution.run_at = await head_of_queue(db, execution.adapter, now=now, excluding=execution.id)
    else:
        execution.run_at = due
    execution.payload = payload
    execution.attempt_count = attempts
    if execution.deadline_at is not None:
        execution.deadline_at = due + timedelta(seconds=budget)
    execution.adapter_state = adapter_state
    execution.last_error = reason[:2000] if reason else None
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.state = "pending"
    execution.updated_at = now
    await db.flush()
    return execution


async def end_hold(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    held_adapter_state: bool = False,
) -> OrchestrationExecution:
    """The held work is starting: close its hold.

    Drops ``payload['hold']`` - and the ``held:<code>`` adapter state and the
    held message when ``held_adapter_state`` - so a hold met later (the run
    crashes and agentic AI is unavailable again) starts its own bound, its
    own attempts and its own runnable budget instead of reusing this one's.
    Owner-checked under the row lock (``refresh``, like :func:`defer`);
    flushes, the caller commits.
    """
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        raise LeaseLostError(f"Execution {execution_id} no longer exists")
    _require_owner(execution, worker_id, require_lease=True)
    if execution.state != "running":
        raise LeaseLostError(f"Execution {execution_id} is {execution.state}")
    if hold_record(execution) is None:
        return execution
    now = datetime.now(UTC)
    _clear_hold(execution)
    if held_adapter_state:
        execution.adapter_state = None
        execution.last_error = None
    execution.updated_at = now
    await db.flush()
    return execution


async def cancel_execution(
    db: AsyncSession, *, execution_id: uuid.UUID
) -> OrchestrationExecution | None:
    """Cancel a non-terminal execution (operator / user action).

    Locks with ``refresh``: a caller may have looked the execution up first, and
    a row that went terminal since must be returned as it is, not cancelled over.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        return None
    if execution.state in TERMINAL_EXECUTION_STATES:
        return execution
    _check_transition(execution.state, "cancelled")
    execution.state = "cancelled"
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.updated_at = now
    await db.flush()
    return execution


async def reset_execution(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    payload: dict,
    deadline_at: datetime | None,
    run_at: datetime | None = None,
) -> OrchestrationExecution | None:
    """Re-engage an existing execution from ANY state as a brand-new run.

    This is the "start is idempotent AND refreshing" primitive behind an
    autonomous run RESTART / RESUME. The OpenAEV run id is stable across
    re-engagements, so its execution dedup key (``autonomous_run:<id>``) is
    stable too, and ``create_execution``'s ``ON CONFLICT DO NOTHING`` would
    otherwise hand back the STALE prior execution (terminal after a cancel, or
    still parked) instead of starting clean. ``reset_execution`` hard-resets the
    SAME row to a fresh ``pending`` engagement: new payload, cycle 0
    (``adapter_state`` cleared), attempts 0, lease + error cleared, and a new
    deadline. Any still-``pending`` outbox events from the prior engagement are
    neutralised (marked ``processed``) so a stale self-resume can't race the
    fresh run.

    Unlike ``requeue_execution`` this bypasses the transition table on purpose:
    it is an operator/lifecycle reset that is valid from a terminal state too.
    Flushes but does not commit — the caller owns the transaction.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id)
    if execution is None:
        return None
    execution.state = "pending"
    execution.payload = payload
    execution.adapter_state = None
    execution.attempt_count = 0
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.last_error = None
    execution.run_at = run_at or now
    execution.deadline_at = deadline_at
    execution.updated_at = now
    # Neutralise the prior engagement's still-pending outbox events (scheduled
    # self-resumes / finding-arrived wakes) so none of them re-arm the freshly
    # reset row a cycle later. "processed" is terminal for the claim query and a
    # valid outbox status (unlike "cancelled").
    await db.execute(
        update(OrchestrationOutboxEvent)
        .where(
            OrchestrationOutboxEvent.execution_id == execution_id,
            OrchestrationOutboxEvent.status == "pending",
        )
        .values(status="processed", processed_at=now)
    )
    await db.flush()
    return execution


# Cap how many run ids go into a single matching query. Each batch expands to
# several ``IN (...)`` predicates (the dedup keys plus three JSONB payload keys),
# so a very large delete (admin purge-all, agent deletion with many runs) would
# otherwise blow past the driver bind-parameter ceiling (asyncpg caps a statement
# at 32767 params) and risk pathological query plans. Batching keeps every query
# small and bounded; matched execution ids are unioned in Python.
_RUN_ID_QUERY_CHUNK = 1000


#: How many generations of the run tree are walked. ``UNION`` stops a cycle,
#: but nothing constrains depth and the reaper walks this for a whole batch —
#: a pathological chain should degrade, not pin a connection. Bounds DEPTH,
#: not width: a generation may be any size.
_MAX_RUN_TREE_DEPTH = 64


async def find_child_runs(db: AsyncSession, run_ids) -> list[uuid.UUID]:
    """Every run *run_ids* handed their turn to, and their own children.

    A read: nothing is locked or written. ``cancel_child_runs`` cancels what it
    returns; a cancel cascade also reads it first, to lock the rows the tree's
    executors hold before it writes any run.
    """
    roots = [u for u in (safe_uuid(str(rid)) for rid in run_ids if rid) if u]
    if not roots:
        return []
    found = (
        await db.execute(
            text(
                """
                WITH RECURSIVE tree AS (
                    SELECT id, 1 AS depth FROM assignment_runs
                     WHERE parent_run_id = ANY(:roots)
                    UNION
                    SELECT r.id, t.depth + 1 FROM assignment_runs r
                      JOIN tree t ON r.parent_run_id = t.id
                     WHERE t.depth < :max_depth
                )
                SELECT id, depth FROM tree
                """
            ).bindparams(roots=roots, max_depth=_MAX_RUN_TREE_DEPTH)
        )
    ).all()
    if any(row.depth >= _MAX_RUN_TREE_DEPTH for row in found):
        # Runs below the cap keep going and neither other arm hears of them,
        # so the task would report CANCELED over work that is still running.
        logger.warning(
            "orchestration: the run tree under %s reached the depth cap (%d); "
            "deeper runs were not cancelled and their partners not told",
            roots,
            _MAX_RUN_TREE_DEPTH,
        )
    return [row.id for row in found]


async def cancel_child_runs(db: AsyncSession, run_ids) -> list[uuid.UUID]:
    """Cancel the runs *run_ids* handed their turn to, and their own children.

    A transfer creates a child ``AssignmentRun`` for the target agent, which is
    neither a ``SubAgentTask`` nor an ``A2ADelegation``. Recursive because a
    child is a full turn of its own and transfers again from there.

    Only non-terminal rows are touched, so how finished work ended is never
    rewritten; their executions go with them, or the reconciler runs the turn
    anyway. Flushes but does not commit.

    Returns every descendant found, cancelled here or already terminal — the
    caller's other arms are scoped by run id and need the whole tree.
    """
    from app.models.assignment import AssignmentRun

    descendants = await find_child_runs(db, run_ids)
    if not descendants:
        return []
    await db.execute(
        update(AssignmentRun)
        .where(
            AssignmentRun.id.in_(descendants),
            AssignmentRun.status.in_(("pending", "delayed", "running")),
        )
        .values(status="cancelled", completed_at=datetime.now(UTC))
    )
    await cancel_executions_for_runs(db, descendants)
    await db.flush()
    return descendants


async def cancel_executions_for_runs(
    db: AsyncSession, run_ids: Iterable[str | uuid.UUID]
) -> int:
    """Cancel every non-terminal execution tied to any of ``run_ids``.

    Deleting an ``AssignmentRun`` (single, bulk, purge, per-assignment, or via
    agent deletion) used to leave its orchestration execution behind. An
    orphaned ``pending`` / ``running`` execution keeps a reconciler concurrency
    slot, gets re-claimed against a vanished row, and — for sub-agent
    delegations — permanently inflates the per-scope fan-out counter
    (``services.sub_agent.count_active_sub_agent_executions``), which silently
    wedges every new delegation for that orchestrator / conversation in
    ``pending`` forever. Cancelling the executions when their runs are deleted
    closes that gap so deleting a stuck run actually frees the slot.

    Matches both the assignment-dispatch execution of each run
    (``assignment_run:<id>`` dedup key / ``payload.run_id``) and the sub-agent
    executions spawned under those runs (``payload.assignment_run_id`` /
    ``payload.parent_run_id`` — the keys the fan-out counter scopes on).

    Flushes but does not commit — the caller owns the transaction. Returns the
    number of executions transitioned to ``cancelled``.
    """
    ids = [str(rid) for rid in run_ids if rid is not None]
    if not ids:
        return 0
    # Match in bounded batches and union the matched ids (a single execution can
    # match on more than one run id — e.g. its own ``assignment_run_id`` and its
    # orchestrator ``parent_run_id`` — so the set also dedupes across batches).
    execution_ids: set[uuid.UUID] = set()
    for batch_ids in chunked(ids, _RUN_ID_QUERY_CHUNK):
        dedup_keys = [f"assignment_run:{rid}" for rid in batch_ids]
        execution_ids.update(
            (
                await db.execute(
                    select(OrchestrationExecution.id).where(
                        OrchestrationExecution.state.in_(("pending", "running")),
                        or_(
                            OrchestrationExecution.dedup_key.in_(dedup_keys),
                            OrchestrationExecution.payload["run_id"].astext.in_(
                                batch_ids
                            ),
                            OrchestrationExecution.payload[
                                "assignment_run_id"
                            ].astext.in_(batch_ids),
                            OrchestrationExecution.payload["parent_run_id"].astext.in_(
                                batch_ids
                            ),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    cancelled = 0
    for execution_id in execution_ids:
        # ``cancel_execution`` re-reads FOR UPDATE and short-circuits a row that
        # raced to a terminal state, so a worker completing/failing between the
        # SELECT above and here is handled safely (not double-counted).
        execution = await cancel_execution(db, execution_id=execution_id)
        if execution is not None and execution.state == "cancelled":
            cancelled += 1
    if cancelled:
        logger.info(
            "Cancelled %d orphaned orchestration execution(s) for %d deleted run(s)",
            cancelled,
            len(ids),
        )
    return cancelled


async def rearm_for_resume(
    db: AsyncSession, *, execution_id: uuid.UUID
) -> OrchestrationExecution | None:
    """Re-arm a paused (AWAIT) ``running`` execution for re-claim by the engine.

    Used by the resume path: an inbound outbox ``resume`` event flips the
    execution back to ``pending`` so ``claim_due`` re-runs it under a fresh
    lease + heartbeat (rather than running the heavy work inside the outbox
    handler's short visibility window). ``attempt_count`` is reset because a
    resume is new work, not a retry of a failure.

    ``adapter_state`` is PRESERVED. ``await_event`` saved the adapter's
    continuation state (e.g. the autonomous cycle counter) when it parked the
    execution, and the adapter must read it back to keep going. Clearing it here
    made the autonomous adapter restart from cycle 0 on every resume, so it
    re-emitted the very same ``cycle-resume:<id>:1`` outbox dedup key; the
    ``ON CONFLICT DO NOTHING`` insert then silently dropped the resume event and
    the run wedged after its first cycle.

    Locks with ``refresh``: the outbox handler read the row to route the event,
    and the state and lease checks below must see a cancel, a deadline sweep or
    another resume's re-claim that landed since - or it revives a terminal run
    or steals a live lease.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id, refresh=True)
    if execution is None:
        return None
    if execution.state != "running":
        # Already terminal / re-armed by a concurrent resume — no-op.
        logger.debug(
            "resume race: rearm on %s in state %s — ignoring",
            execution_id,
            execution.state,
        )
        return execution
    if execution.lease_owner is not None:
        # The execution is still actively held by a worker (the resume raced
        # ahead of the AWAIT pause). A paused execution always has its lease
        # released, so a present lease_owner means re-arming now would steal a
        # live lease and cause a duplicate run. Signal the outbox to retry once
        # the worker has released the lease and the execution is truly paused.
        logger.debug(
            "resume race: rearm on %s still lease-held by %r — deferring",
            execution_id,
            execution.lease_owner,
        )
        raise ResumeNotReadyError(
            f"Execution {execution_id} still lease-held; resume not ready"
        )
    _check_transition(execution.state, "pending")
    execution.state = "pending"
    execution.attempt_count = 0
    # adapter_state is intentionally left as-is (see docstring): it carries the
    # continuation state the adapter saved at await_event time.
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.run_at = now
    execution.updated_at = now
    await db.flush()
    return execution


async def requeue_execution(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    run_at: datetime | None = None,
) -> OrchestrationExecution | None:
    """Operator retry: reset a ``failed`` / ``dead_letter`` row to ``pending``.

    Clears the attempt counter and lease so the reconciler re-runs it cleanly.
    This is the only path that exercises ``failed | dead_letter -> pending``.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id)
    if execution is None:
        return None
    _check_transition(execution.state, "pending")
    execution.state = "pending"
    execution.attempt_count = 0
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.last_error = None
    execution.adapter_state = None
    execution.run_at = run_at or now
    execution.updated_at = now
    await db.flush()
    return execution


async def relaunch_dead_lettered(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    deadline_at: datetime,
    reason: str,
    payload_update: dict | None = None,
    adapter_state: str | None = None,
) -> OrchestrationExecution | None:
    """Put a ``dead_letter`` row back to ``pending``, first in its adapter's queue, on its next attempt.

    Unlike :func:`requeue_execution` (an operator retry, its attempts reset), the attempts it used are kept: the
    next claim counts one more, so ``max_attempts`` still bounds it. Its deadline is moved out to *deadline_at* (never
    in). For an adapter whose dead-lettered work still has an attempt left and a way to run again (a recording
    relaunched after its worker stopped, dead-lettered on its deadline). ``None`` when the row is gone or no longer
    ``dead_letter``. Flushes; the caller commits.
    """
    now = datetime.now(UTC)
    execution = await _locked_get(db, execution_id)
    if execution is None or execution.state != "dead_letter":
        return None
    _check_transition(execution.state, "pending")
    execution.state = "pending"
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.adapter_state = adapter_state
    execution.last_error = reason[:2000]
    if execution.deadline_at is None or execution.deadline_at < deadline_at:
        execution.deadline_at = deadline_at
    if payload_update:
        execution.payload = {**(execution.payload or {}), **payload_update}
    execution.run_at = await head_of_queue(db, execution.adapter, now=now, excluding=execution.id)
    execution.updated_at = now
    await db.flush()
    return execution


# ── Recovery sweeps ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeaseExpiry:
    """What the engine saw when it took an expired lease back.

    A lease that ended, and when its holder was last heard from: never a
    crash, which the engine cannot observe. Only a process that saw the exit
    (the render supervisor names a render process's exit code or signal) may
    say that.
    """

    #: The holder's last write of the row: its claim, a heartbeat or a checkpoint.
    last_heartbeat_at: datetime
    #: Seconds from that write to the reclaim.
    silent_seconds: int
    owner: str | None
    #: The attempt the expiry ended.
    attempt: int
    #: Why it was taken back before its lease ran out: its worker is gone, in words.
    why: str | None = None
    #: What that worker last said of itself (``worker_presence``): its version, its memory, its state.
    owner_record: dict | None = None

    def describe(self) -> str:
        return f"last heartbeat {self.silent_seconds} s ago"


def lease_expiry(execution: OrchestrationExecution) -> LeaseExpiry | None:
    """The lease expiry that ended the execution's current attempt, or ``None``.

    An expiry recorded for an earlier attempt is not this one's: the re-run
    that followed it ended another way.
    """
    record = (execution.payload or {}).get(LEASE_EXPIRY_PAYLOAD_KEY)
    if not isinstance(record, dict) or record.get("attempt") != execution.attempt_count:
        return None
    try:
        heard_at = datetime.fromisoformat(str(record["last_heartbeat_at"]))
        silent = int(record["silent_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    owner = record.get("owner")
    why, owner_record = record.get("why"), record.get("owner_record")
    return LeaseExpiry(
        last_heartbeat_at=heard_at if heard_at.tzinfo else heard_at.replace(tzinfo=UTC),
        silent_seconds=silent,
        owner=owner if isinstance(owner, str) else None,
        attempt=execution.attempt_count,
        why=why if isinstance(why, str) else None,
        owner_record=owner_record if isinstance(owner_record, dict) else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def last_heard_at(execution: OrchestrationExecution) -> datetime | None:
    """When the holder of the execution's lease was last heard from: its last write, ``None`` with no lease.

    Only the holder writes a leased row - its claim, a heartbeat, a
    checkpoint - so its last write is the last time it was heard from.
    """
    if execution.lease_expires_at is None:
        return None
    if execution.updated_at is not None:
        return _aware(execution.updated_at)
    return _aware(execution.lease_expires_at) - timedelta(seconds=settings.orchestration_lease_seconds)


@dataclass(frozen=True)
class LeaseHeartbeat:
    """How a running execution's lease stands now: what a caller waiting on its work is told."""

    #: Seconds since its holder last renewed it (:func:`last_heard_at`).
    silent_seconds: int
    #: Seconds until it expires; 0 once it has.
    expires_in_seconds: int
    attempt: int
    max_attempts: int
    #: The lease its holder took at its last renewal: an adapter may hold a longer one than the engine's.
    lease_seconds: int = 0
    #: The worker holding it.
    owner: str | None = None
    #: The attempts given back for a worker gone for a reason outside the work (:data:`INFRA_RELAUNCHES_KEY`).
    infra_relaunches: int = 0
    #: Of those, the workers killed by a rollout (:data:`ROLLOUT_KILLS_KEY`).
    rollout_kills: int = 0

    @property
    def late(self) -> bool:
        """At least one renewal missed: more than half the lease passed since the last one (renewed every third)."""
        return self.silent_seconds * 2 > (self.lease_seconds or settings.orchestration_lease_seconds)

    def as_dict(self) -> dict:
        return {
            "holder": self.owner,
            "last_heartbeat_seconds_ago": self.silent_seconds,
            "expires_in_seconds": self.expires_in_seconds,
            "lease_seconds": self.lease_seconds,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "infra_relaunches": self.infra_relaunches,
            "rollout_kills": self.rollout_kills,
        }


def lease_heartbeat(execution: OrchestrationExecution, now: datetime | None = None) -> LeaseHeartbeat | None:
    """How the execution's lease stands at *now*, or ``None`` when it is not running under one."""
    heard_at = last_heard_at(execution)
    if execution.state != "running" or heard_at is None:
        return None
    now = now or datetime.now(UTC)
    expires_at = _aware(execution.lease_expires_at)
    return LeaseHeartbeat(
        silent_seconds=max(0, int((now - heard_at).total_seconds())),
        expires_in_seconds=max(0, int((expires_at - now).total_seconds())),
        attempt=execution.attempt_count or 0,
        max_attempts=execution.max_attempts or 0,
        # Each renewal writes both the expiry and ``updated_at``: their gap is the lease it took.
        lease_seconds=max(0, round((expires_at - heard_at).total_seconds())),
        owner=getattr(execution, "lease_owner", None),
        infra_relaunches=infra_relaunches(execution),
        rollout_kills=rollout_kills(execution),
    )


#: The ``adapter_state`` of an execution taken back from a worker gone (``reclaim_from_gone_owners``).
OWNER_GONE_STATE = "worker_gone"

#: The presence states (``worker_presence.STATE_DRAINING`` / ``STATE_STOPPED``) of a worker that said it was stopping.
_STOPPING_STATES = frozenset({"draining", "stopped"})


#: The key ``worker_presence.gone`` adds to the record of a worker gone without a word that a deploy replaced.
REPLACED_BY_DEPLOY_KEY = "replaced_by_deploy"

#: The payload key counting the attempts given back to an execution because its worker went for a reason outside
#: its work (a rollout, a restart, a worker lost without a stop request and with no sign of an out-of-memory kill).
INFRA_RELAUNCHES_KEY = "infra_relaunches"

#: How many silent worker losses (no stop request, no out-of-memory sign) an execution is given its attempt back for.
#: Past it, each loss spends an attempt again: a crash that repeats is likelier the work's own. Counted on
#: :data:`SILENT_LOSSES_KEY` alone: the handovers on a stop request and the rollout kills have their own count.
MAX_SILENT_LOSSES_GIVEN_BACK = 6

#: How many rollout kills (a worker of an older release than the live one gone without a stop request and with no
#: out-of-memory sign) an execution is given its attempt back for: a run of back-to-back deploys kills many.
MAX_ROLLOUT_KILLS_GIVEN_BACK = 24

#: The payload keys counting, apart, the silent losses and the rollout kills given back (each also counts in
#: :data:`INFRA_RELAUNCHES_KEY`).
SILENT_LOSSES_KEY = "silent_losses"
ROLLOUT_KILLS_KEY = "rollout_kills"

#: What a ``silent_loss_given_back`` classifier answers for a silent loss: a rollout kill, or another loss given back.
LOSS_ROLLOUT_KILL = "rollout_kill"
LOSS_SILENT = "silent_loss"


def infra_relaunches(execution: OrchestrationExecution) -> int:
    """How many attempts *execution* was given back for a worker gone for a reason outside its work."""
    return _payload_count(execution, INFRA_RELAUNCHES_KEY)


def rollout_kills(execution: OrchestrationExecution) -> int:
    """How many of :func:`infra_relaunches` were a worker killed by a rollout (:data:`ROLLOUT_KILLS_KEY`)."""
    return _payload_count(execution, ROLLOUT_KILLS_KEY)


def _payload_count(execution: OrchestrationExecution, key: str) -> int:
    value = (getattr(execution, "payload", None) or {}).get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _loss_kind(answer: object) -> str | None:
    """The kind of a silent loss a classifier answered: ``True`` (the older contract) reads as :data:`LOSS_SILENT`."""
    if answer is True:
        return LOSS_SILENT
    return answer if answer in (LOSS_ROLLOUT_KILL, LOSS_SILENT) else None


def stopped_on_its_word(owner_record: dict | None) -> bool:
    """Whether the gone worker of *owner_record* said it was stopping before it went.

    A worker asked to stop publishes ``draining`` then ``stopped``; one that
    crashed or was killed leaves its last ``running`` record to lapse. A worker
    of an older release gone without a word, with no sign of an out-of-memory
    kill, was killed by its deploy (:data:`REPLACED_BY_DEPLOY_KEY`): a
    stop outside the work, read the same way.
    """
    if not isinstance(owner_record, dict):
        return False
    return owner_record.get("state") in _STOPPING_STATES or owner_record.get(REPLACED_BY_DEPLOY_KEY) is True


def killed_by_rollout(owner_record: dict | None) -> bool:
    """Whether the gone worker of *owner_record* went without a word, killed by a deploy
    (:data:`REPLACED_BY_DEPLOY_KEY`): one of :func:`stopped_on_its_word`, but no hand-back was even tried."""
    return (
        isinstance(owner_record, dict)
        and owner_record.get("state") not in _STOPPING_STATES
        and owner_record.get(REPLACED_BY_DEPLOY_KEY) is True
    )


async def _take_back(
    db: AsyncSession,
    row: OrchestrationExecution,
    now: datetime,
    *,
    why: str,
    head: bool,
    owner_record: dict | None = None,
    bound_of: Callable[[str, int], int] | None = None,
    heard_at: datetime | None = None,
    silent_loss_given_back: Callable[[str, dict], str | bool | None] | None = None,
) -> bool:
    """Put *row*, whose holder no longer has it, back to ``pending`` - or ``dead_letter`` with no attempt left; whether
    it was dead-lettered. *why* says what ended the attempt; *head* puts it first in its adapter's queue.

    *bound_of* (adapter name, ``max_attempts``) -> the bound its adapter runs that row under now
    (``OrchestrationAdapter.raised_attempt_bound``): read before the exhaustion check, under the row lock of
    this take-back, so a row queued under an older bound is not dead-lettered at it before its next claim.
    *heard_at*: when its holder was last heard from, for a row with no lease left to read it from.
    *silent_loss_given_back* (adapter name, holder's last record) -> how a holder gone without a stop request
    gives the attempt back (``OrchestrationAdapter.gives_back_silent_losses`` and no out-of-memory sign):
    :data:`LOSS_ROLLOUT_KILL` (a worker of an older release, killed by a rollout) up to
    :data:`MAX_ROLLOUT_KILLS_GIVEN_BACK` times, :data:`LOSS_SILENT` up to :data:`MAX_SILENT_LOSSES_GIVEN_BACK`
    times, each on its own count, every one counted apart from the attempts (:data:`INFRA_RELAUNCHES_KEY`); a falsy
    answer spends the attempt.
    """
    if bound_of is not None:
        raised = bound_of(row.adapter, row.max_attempts)
        if isinstance(raised, int) and raised > row.max_attempts:
            row.max_attempts = raised
    owner = row.lease_owner
    heard_at = heard_at or last_heard_at(row) or now
    silent = max(0, int((now - heard_at).total_seconds()))
    row.lease_owner = None
    row.lease_expires_at = None
    row.adapter_state = OWNER_GONE_STATE if owner_record is not None else None
    row.updated_at = now
    held_attempts = (hold_record(row) or {}).get("attempts")
    if isinstance(held_attempts, int):
        # Lost during a held execution's re-check: that claim was
        # not an attempt, and ``defer`` would have given it back. Still
        # bounded: past the hold's bound the adapter stops holding and
        # ``sweep_deadlines`` no longer gives it its budget back.
        row.attempt_count = min(row.attempt_count, held_attempts)
    elif stopped_on_its_word(owner_record):
        # Its worker said it was stopping (a rollout, a node drain):
        # the attempt ended for a reason outside the work, so it is given
        # back. A worker gone without a word (a crash, an OOM kill) may have
        # died of that very work and still spends it.
        row.attempt_count = max(0, row.attempt_count - 1)
        row.payload = {**(row.payload or {}), INFRA_RELAUNCHES_KEY: infra_relaunches(row) + 1}
    elif owner_record is not None and silent_loss_given_back is not None:
        # Gone without a stop request and with no sign of an out-of-memory kill, on an adapter whose work keeps
        # its progress across workers (a render resumes from its kept frames): a worker of an older release killed
        # by a rollout, or a node lost, not the work's own fault. Each kind given back a bounded number of times on
        # its own count - the handovers on a stop request never use up the bound of the silent losses -, counted
        # apart from its attempts.
        kind = _loss_kind(silent_loss_given_back(row.adapter, owner_record))
        key, bound = (
            (ROLLOUT_KILLS_KEY, MAX_ROLLOUT_KILLS_GIVEN_BACK)
            if kind == LOSS_ROLLOUT_KILL
            else (SILENT_LOSSES_KEY, MAX_SILENT_LOSSES_GIVEN_BACK)
        )
        if kind is not None and _payload_count(row, key) < bound:
            row.attempt_count = max(0, row.attempt_count - 1)
            row.payload = {
                **(row.payload or {}),
                INFRA_RELAUNCHES_KEY: infra_relaunches(row) + 1,
                key: _payload_count(row, key) + 1,
            }
    expiry = {
        "at": now.isoformat(),
        "last_heartbeat_at": heard_at.isoformat(),
        "silent_seconds": silent,
        "owner": owner,
        "attempt": row.attempt_count,
    }
    if owner_record is not None:
        expiry["why"] = why
        expiry["owner_record"] = owner_record
    row.payload = {**(row.payload or {}), LEASE_EXPIRY_PAYLOAD_KEY: expiry}
    heard = f"last heartbeat {silent} s ago"
    if row.attempt_count >= row.max_attempts:
        row.state = "dead_letter"
        row.last_error = f"{why}, {heard}, with no attempt left."
    else:
        row.state = "pending"
        row.run_at = await head_of_queue(db, row.adapter, now=now, excluding=row.id) if head else now
        row.last_error = f"{why}, {heard}: run again ({row.attempt_count} of {row.max_attempts} attempts used)."
    taken = f"the lease of {owner} expired" if owner_record is None else f"taken back from {owner}: {why}"
    logger.warning(
        "Execution %s (%s): %s, %s -> %s (attempt %d of %d)",
        row.id,
        row.adapter,
        taken,
        heard,
        row.state,
        row.attempt_count,
        row.max_attempts,
    )
    return row.state == "dead_letter"


async def reclaim_expired_leases(
    db: AsyncSession,
    *,
    limit: int,
    adapter: str | None = None,
    head_adapters: Iterable[str] = (),
    bound_of: Callable[[str, int], int] | None = None,
    now: datetime | None = None,
) -> list[OrchestrationExecution]:
    """Recover ``running`` rows whose lease expired: a worker that died, or one
    that stopped renewing it. *bound_of*: as :func:`_take_back`. *now*: the
    cycle's, shared with :func:`reclaim_from_gone_owners` so a lease that
    expires between the two sweeps is not taken here without its holder's word.

    Rows with attempts remaining go back to ``pending`` (re-runnable now - first
    in the queue for the adapters of *head_adapters*, which resume what a
    worker had in flight before anything else); exhausted rows go to
    ``dead_letter``. Returns the dead-lettered rows so the
    reconciler can invoke ``adapter.on_dead_letter`` for cleanup. Lease-less
    paused (AWAIT) rows are NOT touched — they wait for an outbox event or the
    deadline sweep. Each row records the expiry (``lease_expiry``) and says in
    ``last_error`` when its holder was last heard from.
    """
    now = now or datetime.now(UTC)
    conditions = [
        OrchestrationExecution.state == "running",
        OrchestrationExecution.lease_expires_at.isnot(None),
        OrchestrationExecution.lease_expires_at < now,
    ]
    if adapter is not None:
        conditions.append(OrchestrationExecution.adapter == adapter)
    stmt = (
        select(OrchestrationExecution)
        .where(*conditions)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    heads = set(head_adapters)
    dead_lettered: list[OrchestrationExecution] = []
    for row in rows:
        if await _take_back(db, row, now, why="Lease expired", head=row.adapter in heads, bound_of=bound_of):
            dead_lettered.append(row)
    await db.flush()
    return dead_lettered


#: What a ``running`` execution taken back with no lease at all says ended its attempt (:func:`reclaim_leaseless`).
NO_LEASE_WHY = "No lease"


async def reclaim_leaseless(
    db: AsyncSession,
    *,
    adapters: Iterable[str],
    quiet_before: datetime,
    limit: int,
    head: bool = True,
    bound_of: Callable[[str, int], int] | None = None,
) -> list[OrchestrationExecution]:
    """Take back the ``running`` executions of *adapters* that hold no lease and were last written before
    *quiet_before*; every row taken back (``state`` says which were dead-lettered).

    Only for adapters that never pause with :func:`await_event` (whose lease-less ``running`` rows wait for an
    event): for them a ``running`` row without a lease has no holder, and neither :func:`reclaim_expired_leases`
    (a lease set and expired) nor :func:`reclaim_from_gone_owners` (a holder named) ever reads it. Each goes
    back to ``pending`` - first in its adapter's queue with *head* - or to ``dead_letter`` with no attempt left,
    through :func:`_take_back`, its last write as when it was last heard from.
    """
    wanted = list(adapters)
    if not wanted:
        return []
    now = datetime.now(UTC)
    stmt = (
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.adapter.in_(wanted),
            OrchestrationExecution.state == "running",
            OrchestrationExecution.lease_expires_at.is_(None),
            OrchestrationExecution.updated_at < quiet_before,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    for row in rows:
        heard = _aware(row.updated_at) if row.updated_at is not None else None
        await _take_back(db, row, now, why=NO_LEASE_WHY, head=head, bound_of=bound_of, heard_at=heard)
    await db.flush()
    return rows


async def leased_owners(db: AsyncSession, *, adapters: Iterable[str], include_expired: bool = False) -> list[str]:
    """The workers holding a live lease on a ``running`` execution of *adapters*.

    *include_expired*: an expired lease's holder too. The take-back of
    workers gone reads their last word before the expired-lease sweep takes
    their executions with none: a worker that said it was stopping and whose
    lease ran out before the next worker started stopped on its word.
    """
    wanted = list(adapters)
    if not wanted:
        return []
    conditions = [
        OrchestrationExecution.adapter.in_(wanted),
        OrchestrationExecution.state == "running",
        OrchestrationExecution.lease_owner.isnot(None),
    ]
    if not include_expired:
        conditions.append(OrchestrationExecution.lease_expires_at >= datetime.now(UTC))
    rows = await db.execute(select(OrchestrationExecution.lease_owner).where(*conditions).distinct())
    return [owner for owner in rows.scalars().all() if owner]


def _renewed_since_its_word(row: OrchestrationExecution, record: dict, now: datetime) -> bool:
    """Whether the holder of *row*, read gone from its presence *record*, renewed the lease since.

    Read under the row lock: a live lease its holder wrote after the last word it
    published (a heartbeat, a checkpoint) means its process still works on it.
    An expired lease, or one never written since that word, is the gone worker's.
    """
    expires = row.lease_expires_at
    if expires is None or _aware(expires) < now:
        return False
    heard = last_heard_at(row)
    try:
        said = datetime.fromisoformat(str(record["at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return heard is not None and heard > _aware(said)


async def reclaim_from_gone_owners(
    db: AsyncSession,
    gone: dict[str, dict],
    *,
    adapters: Iterable[str],
    limit: int,
    reason_of=None,
    bound_of: Callable[[str, int], int] | None = None,
    silent_loss_given_back: Callable[[str, dict], str | bool | None] | None = None,
    waits_for_lease: Callable[[str, dict], bool] | None = None,
    now: datetime | None = None,
) -> list[OrchestrationExecution]:
    """Take back at once the ``running`` executions of *adapters* leased by a worker that is gone.

    *bound_of*, *silent_loss_given_back*: as :func:`_take_back`. *waits_for_lease* (adapter name, holder's last
    record) -> whether that holder may still be writing to the work after it went: its row is left until its
    lease expires, then taken back on the same last word.

    *gone* maps each holder known gone (``worker_presence.gone``) to its last
    record; *reason_of* words why it went (``worker_presence.stop_reason``).
    Its lease would only run out minutes later, while nothing works on it: the
    execution goes back to ``pending``, first in its adapter's queue, the
    attempt the worker had counted given back when that worker said it was
    stopping (:func:`stopped_on_its_word`) and kept otherwise (it may
    have died of that very work), or to ``dead_letter`` with none left. Each row records the holder's last record
    (``lease_expiry(...).owner_record``) for what resumes it to say. Returns the
    dead-lettered rows.
    """
    wanted = list(adapters)
    if not gone or not wanted:
        return []
    now = now or datetime.now(UTC)
    stmt = (
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.adapter.in_(wanted),
            OrchestrationExecution.state == "running",
            OrchestrationExecution.lease_owner.in_(list(gone)),
            # A row past its deadline is the deadline sweep's: it dead-letters it on its deadline, never as a
            # lease taken back (the reconciler runs this take-back first).
            or_(OrchestrationExecution.deadline_at.is_(None), OrchestrationExecution.deadline_at >= now),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    dead_lettered: list[OrchestrationExecution] = []
    for row in rows:
        record = gone.get(row.lease_owner or "") or {}
        if _renewed_since_its_word(row, record, now):
            # Its holder wrote the row after its last word: alive after all. Left to it; the next cycle decides
            # on fresh presence and lease.
            continue
        if (
            waits_for_lease is not None
            and row.lease_expires_at is not None
            and _aware(row.lease_expires_at) >= now
            and waits_for_lease(row.adapter, record)
        ):
            # Its holder may still be writing to the work: taken back once its lease ran out.
            continue
        why = reason_of(record) if reason_of is not None else "Its worker is gone"
        why = why[:1].upper() + why[1:]
        if await _take_back(
            db,
            row,
            now,
            why=why,
            head=True,
            owner_record=record,
            bound_of=bound_of,
            silent_loss_given_back=silent_loss_given_back,
        ):
            dead_lettered.append(row)
    await db.flush()
    return dead_lettered


async def sweep_deadlines(
    db: AsyncSession,
    *,
    limit: int,
    adapter: str | None = None,
) -> list[OrchestrationExecution]:
    """Dead-letter ``pending`` / ``running`` rows past their ``deadline_at``.

    Catches both "nobody ever claimed it" and "worker held the lease across the
    deadline" (and "waited on a callback that never arrived"). Returns the rows
    so the reconciler can invoke ``adapter.on_dead_letter``. Work created by
    :func:`queued_deadline` that no worker ever started is marked
    :data:`EXPIRED_BEFORE_START_PAYLOAD_KEY` with its queue wait
    (:func:`expired_before_start`).
    """
    now = datetime.now(UTC)
    conditions = [
        OrchestrationExecution.state.in_(["pending", "running"]),
        OrchestrationExecution.deadline_at.isnot(None),
        OrchestrationExecution.deadline_at < now,
    ]
    if adapter is not None:
        conditions.append(OrchestrationExecution.adapter == adapter)
    stmt = (
        select(OrchestrationExecution)
        .where(*conditions)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    dead_lettered: list[OrchestrationExecution] = []
    for row in rows:
        budget = (hold_record(row) or {}).get("budget_seconds")
        since = hold_started_at(row)
        if (
            isinstance(budget, (int, float))
            and since is not None
            and (now - since).total_seconds() < settings.ai_unavailable_hold_max_seconds
        ):
            # Held and past its deadline only because nothing claimed
            # it meanwhile (worker or reconciler down): the wait is not
            # runnable time. ``claim_due`` refuses a row past its deadline, so
            # it gets its runnable budget again, and the hold's own bound
            # decides at the next re-check - or here, once it is over.
            row.deadline_at = now + timedelta(seconds=budget)
            if row.lease_expires_at is None:
                row.updated_at = now
            else:
                # Only the holder writes a leased row's ``updated_at``: it is when that holder was last heard
                # from (:func:`last_heard_at`), read by the take-back of workers gone. Kept as it is in the
                # UPDATE, so the column's ``onupdate`` does not stamp it either.
                flag_modified(row, "updated_at")
            continue
        # deadline race: only dead_letter / cancelled are legal past deadline.
        row.state = "dead_letter"
        payload = row.payload or {}
        if isinstance(payload.get(START_BUDGET_PAYLOAD_KEY), (int, float)) and not payload.get(STARTED_AT_PAYLOAD_KEY):
            # Its queue-wait bound, not its execution budget: no worker ever started it. Recorded apart
            # (``deadline_exceeded_before_start``) so the adapter fails it without a model call, without
            # ``run.failed`` and outside the failure counts.
            due = _aware(row.run_at) if row.run_at is not None else now
            row.payload = {
                **payload,
                EXPIRED_BEFORE_START_PAYLOAD_KEY: True,
                QUEUE_WAIT_PAYLOAD_KEY: round(max(0.0, (now - due).total_seconds()), 3),
            }
            row.last_error = expired_before_start_message("The work", row)
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            dead_lettered.append(row)
            continue
        # The worker that held it, when one did: a deadline reached under a live holder reads apart from one
        # reached in the queue.
        held_by = f" (held by {row.lease_owner})" if row.lease_owner else ""
        row.last_error = (
            f"Deadline exceeded at {row.deadline_at.isoformat()}{held_by}."
            if row.deadline_at
            else f"Deadline exceeded{held_by}."
        )
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = now
        dead_lettered.append(row)
    await db.flush()
    return dead_lettered


# ── Transactional outbox ──────────────────────────────────────────────────────


async def emit_outbox_event(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    event_type: str,
    dedup_key: str,
    payload: dict | None = None,
    available_at: datetime | None = None,
) -> uuid.UUID | None:
    """Emit an outbox event idempotently.

    Race-safe via ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` against the
    ``(execution_id, event_type, dedup_key)`` unique constraint — a concurrent
    duplicate short-circuits cleanly without poisoning the transaction. Returns
    the new id, or ``None`` if the event already existed.
    """
    now = datetime.now(UTC)
    stmt = (
        pg_insert(OrchestrationOutboxEvent)
        .values(
            id=uuid.uuid4(),
            execution_id=execution_id,
            event_type=event_type,
            dedup_key=dedup_key,
            payload=payload or {},
            status="pending",
            available_at=available_at or now,
            attempts=0,
            max_attempts=5,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_orchestration_outbox_events_dedup")
        .returning(OrchestrationOutboxEvent.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def claim_outbox_events(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    visibility_seconds: int | None = None,
) -> list[OrchestrationOutboxEvent]:
    """Claim due ``pending`` outbox events (SKIP LOCKED).

    Each claim increments ``attempts`` and pushes ``available_at`` forward by a
    visibility timeout, so a worker that crashes mid-handle releases the event
    for re-delivery without losing the attempt count. An event that exceeds
    ``max_attempts`` is dead-lettered in place and not returned for processing.
    """
    now = datetime.now(UTC)
    visibility_seconds = visibility_seconds or settings.orchestration_lease_seconds
    stmt = (
        select(OrchestrationOutboxEvent)
        .where(
            OrchestrationOutboxEvent.status == "pending",
            OrchestrationOutboxEvent.available_at <= now,
        )
        .order_by(OrchestrationOutboxEvent.available_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    claimed: list[OrchestrationOutboxEvent] = []
    for row in rows:
        row.attempts += 1
        if row.attempts > row.max_attempts:
            row.status = "dead_letter"
            row.last_error = row.last_error or "Max delivery attempts exceeded."
            row.processed_at = now
            continue
        row.available_at = now + timedelta(seconds=visibility_seconds)
        claimed.append(row)
    await db.flush()
    return claimed


async def mark_outbox_processed(db: AsyncSession, *, event_id: uuid.UUID) -> None:
    """Terminally mark an outbox event ``processed``."""
    now = datetime.now(UTC)
    event = await _locked_get_outbox(db, event_id)
    if event is None or event.status != "pending":
        return
    event.status = "processed"
    event.processed_at = now
    await db.flush()


async def mark_outbox_failed(
    db: AsyncSession, *, event_id: uuid.UUID, error: str
) -> None:
    """Record an outbox handler failure: backoff retry or dead-letter.

    ``attempts`` was already incremented at claim time, so exhaustion is
    ``attempts >= max_attempts`` here.
    """
    now = datetime.now(UTC)
    event = await _locked_get_outbox(db, event_id)
    if event is None or event.status != "pending":
        return
    event.last_error = error[:2000] if error else None
    if event.attempts >= event.max_attempts:
        event.status = "dead_letter"
        event.processed_at = now
    else:
        event.available_at = now + timedelta(seconds=_backoff_seconds(event.attempts))
    await db.flush()


# ── Reads + wake ──────────────────────────────────────────────────────────────


async def get_execution(
    db: AsyncSession, execution_id: uuid.UUID
) -> OrchestrationExecution | None:
    """Non-locking read of an execution (for adapters / API / tests)."""
    return (
        await db.execute(
            select(OrchestrationExecution).where(
                OrchestrationExecution.id == execution_id
            )
        )
    ).scalar_one_or_none()


async def get_execution_by_dedup_key(
    db: AsyncSession, dedup_key: str, *, for_update: bool = False
) -> OrchestrationExecution | None:
    """Read the (unique) execution carrying ``dedup_key``.

    Non-locking by default. ``for_update=True`` locks the row and reads its
    current values over the identity map (the ``_locked_get(refresh=True)``
    read), for a caller that decides something from the row - who may act on
    it - and then transitions it in the same transaction: the decision is made
    on the values the transition sees, not on a row another transaction may
    have rewritten in between.
    """
    stmt = select(OrchestrationExecution).where(
        OrchestrationExecution.dedup_key == dedup_key
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def notify_orchestration_wake() -> None:
    """Nudge the reconciler to run a cycle immediately (best-effort).

    Call after committing a producer transaction that created a due-now
    execution or outbox event. Failure is non-fatal — the reconciler's poll
    interval is the correctness backstop.
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.publish(ORCHESTRATION_WAKE_CHANNEL, "1")
    except Exception:
        logger.debug(
            "orchestration wake publish failed; reconciler will poll", exc_info=True
        )
