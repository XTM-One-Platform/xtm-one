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

"""Orchestration reconciler — the single drive loop for the durable engine.

Runs in the worker process. Every cycle (or on a Redis "wake" nudge) it:

  1. sweeps deadline-exceeded executions to ``dead_letter`` (+ ``on_dead_letter``),
  2. reclaims expired leases (crashed workers) back to ``pending`` / ``dead_letter``,
  3. claims due ``pending`` executions per adapter and runs each as a supervised,
     lease-heartbeated, deadline-bounded task,
  4. runs each adapter's ``periodic`` domain sweep,
  5. drives pending transactional-outbox events through ``handle_event``.

Lease-based ``FOR UPDATE SKIP LOCKED`` claiming makes the loop safe to run on
any number of worker replicas concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.core.cancellation import run_to_completion
from app.core.config import settings
from app.core.database import async_session
from app.core.turn_writer import TurnSuperseded, single_writer
from app.core.utils import format_exception_message
from app.llm.provider_access import acting_for
from app.models.orchestration import (
    TERMINAL_EXECUTION_STATES,
    OrchestrationExecution,
)
from app.services import orchestration_engine as engine
from app.services import worker_presence
from app.services.orchestration_adapters import (
    AdapterContext,
    AdapterNotFoundError,
    get_adapter,
    register_builtin_adapters,
    registered_adapters,
)

logger = logging.getLogger(__name__)

# Strong refs to in-flight execution tasks so they aren't GC'd mid-run.
_execution_tasks: set[asyncio.Task] = set()
#: The executions running here whose adapter stops on a lost lease (``stop_on_lease_loss``), by execution: their
#: cancel event and their task, fired and awaited by :func:`_stop_lease_loss_work` before this worker says it stopped.
_lease_loss_events: dict[uuid.UUID, tuple[asyncio.Event, asyncio.Task | None]] = {}
#: How long a stopping worker waits, in all, for the executions it told to stop and those still committing
#: before it says it stopped: one deadline for both waits, inside the shutdown headroom.
LEASE_LOSS_STOP_WAIT_SECONDS = 5.0
_semaphore: asyncio.Semaphore | None = None
# Dedicated per-adapter semaphores for adapters that declare a positive
# ``concurrency_limit`` (e.g. ``autonomous_pentest``). SEPARATE from the shared
# ``_semaphore`` pool so a burst of one long-lived workload can't starve the
# rest. Built lazily inside the worker event loop, keyed by adapter name.
_adapter_semaphores: dict[str, asyncio.Semaphore] = {}
# The permits each dedicated semaphore was built or grown to: it grows with the adapter's ``slots_added``,
# never shrinks - the claim bound (``_available_slots_for``) is what holds the adapter to its slots of the moment.
_adapter_semaphore_sizes: dict[str, int] = {}
# In-flight execution-task count per adapter, so each adapter's per-cycle claim
# is bounded by its OWN free capacity (its dedicated budget when it has one; the
# shared pool minus every dedicated adapter's in-flight otherwise).
_inflight_by_adapter: dict[str, int] = {}
# The executions in flight here in another adapter's idle slot: execution id -> (the lending adapter, the
# event that asks it to give that slot back, ``AdapterContext.give_back``).
_lent: dict[uuid.UUID, tuple[str, asyncio.Event]] = {}
# How long a job a lane takes its lent slots back for waits, due, before a slot is taken back: long enough
# for a worker with a free slot of that lane to claim it first.
_TAKE_BACK_AFTER_SECONDS = 15.0
_stop_event: asyncio.Event | None = None
# Set once a stop was asked: the worker drains - claims nothing new, its
# executions run on - until the stop itself (``_stop_event``) hands back what is
# still running.
_draining: asyncio.Event | None = None
# The signal that asked the worker to stop, when it was seen (``note_stop_signal``), and when a stop was first
# asked (``stop_asked_at``): what a stopping worker's waits are counted from.
_stop_signal: str | None = None
_stop_asked_at: float | None = None


@dataclass
class _SlotHolder:
    """An execution running in its own adapter's slot on this worker, for an adapter that keeps its slots
    (``OrchestrationAdapter.keeps_slot_through``): when it first took a slot, and the event that gives it back."""

    adapter: str
    since: datetime
    give_back: asyncio.Event = field(default_factory=asyncio.Event)


#: The executions this worker runs in their own adapter's slot, for the adapters that keep their slots.
_slot_holders: dict[uuid.UUID, _SlotHolder] = {}
#: How long an execution that lost its slot to a stopping or a gone worker waits, due, before a worker with no slot
#: free takes one back for it: a cycle or two for a worker with a free slot to claim it first.
GIVE_BACK_AFTER_SECONDS = 15.0
#: How long the worker that gave a slot back keeps the execution waiting for it in hand (``claim_for``): only it
#: claims it meanwhile, anyone after - a worker that went meanwhile never strands it.
GIVE_BACK_HOLD_SECONDS = 60.0
#: How long work a stopping worker hands back to the head of the queue is held for a running worker of the
#: incoming release (``claim_for``): only it claims the work that long, any worker after. A worker of the
#: stopping one's own release is stopped by the same rolling update next, so work it claimed was handed back
#: once per old worker: one agent run handed back 9 times in 40 minutes (#8819).
HANDBACK_INCOMING_HOLD_SECONDS = 90.0
#: How long work a stopping worker hands back while no worker of the incoming release runs yet is kept from the
#: workers of its own release (``not_release``): a rolling update stops them next, and a render one of them took was
#: handed back again at 23 % through two rollouts (5 Nov 2026). Past it any worker claims the work: a plain restart,
#: whose new workers run the same release, waits at most this long.
HANDBACK_OUTGOING_HOLD_SECONDS = 300.0

# Upper bound (seconds) on the in-process DB-pool dispose recovery, so a
# dispose that blocks closing dead connections to an unreachable DB can never
# wedge the reconciler loop itself.
_POOL_DISPOSE_TIMEOUT = 15

# Retry parameters for ``run_dead_letter``. Transient DB errors (connection
# reset, pool exhaustion) can cause the first ``on_dead_letter`` attempt to
# fail and leave the backing run stuck in an active status.  A short
# exponential-backoff retry loop makes the sync hook resilient to such blips
# before falling back to the stale-sweep safety net in autonomous.py.
#
# The budget is deliberately TIGHT: ``run_dead_letter`` is awaited inline from
# the reconciler cycle, and the per-pod liveness heartbeat is only refreshed
# between cycles (``reconciler_heartbeat_max_age_seconds``, default 120s). A
# worst case of 2 sweeps x ``orchestration_claim_batch`` (default 20) dead
# letters per cycle, each burning the full 0.5s + 1s = 1.5s backoff on a
# systematic adapter failure, adds ~60s to the cycle — safely inside the
# heartbeat allowance. Longer outages (a real DB failover) are NOT the retry
# loop's job: the cycle fails earlier in that case, and the stale sweep
# reconciles the stranded run once the DB returns.
_DEAD_LETTER_MAX_RETRIES: int = 2
_DEAD_LETTER_RETRY_BASE_DELAY: float = 0.5  # seconds; doubles on each attempt


def _is_transient_dead_letter_error(exc: BaseException) -> bool:
    """Return True when an ``on_dead_letter`` failure is worth retrying.

    Only connection / pool / timeout classes are transient: retrying a
    deterministic adapter failure (corrupt payload, adapter bug) would burn
    the full backoff on every cycle for no benefit. SQLAlchemy wraps driver
    errors, so match its transient wrapper types plus raw network/timeout
    errors; ``DBAPIError`` counts only when SQLAlchemy itself flagged the
    connection as invalidated (a disconnect mid-statement) — or when the
    driver reports a serialization failure, below.
    """
    # Deadlock (40P01) and serialization failure (40001) succeed on retry. They
    # arrive as a PLAIN ``DBAPIError`` with ``connection_invalidated`` False
    # (PostgreSQL 17 via asyncpg), so the checks below miss them and a
    # deadlocked ``on_dead_letter`` would burn its attempt.
    # ``sqlstate`` is asyncpg's spelling, ``pgcode`` psycopg's: read both, or a
    # driver change silently makes deadlocks non-retryable.
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in {"40P01", "40001"}:
        return True
    if isinstance(
        exc,
        (
            OperationalError,  # SQLAlchemy: connection refused/reset, restarts
            InterfaceError,  # SQLAlchemy: driver-level connection loss
            SQLAlchemyTimeoutError,  # SQLAlchemy: pool checkout timeout
            ConnectionError,  # raw socket resets/refusals from the driver
            TimeoutError,  # raw network timeouts (asyncio alias included)
        ),
    ):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


def _make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _write_heartbeat() -> None:
    """Touch the per-pod reconciler heartbeat file (best-effort).

    The reconciler keeps this fresh while it is alive and either completing
    cycles or actively retrying + self-recovering from a transient failure, so
    a short DB outage / failover does NOT cause restart churn — the worker
    waits and heals in-process (see ``reconciler_loop`` and
    ``_recover_db_pool``). The heartbeat is only allowed to go stale once the
    reconciler has been unable to complete a single cycle for
    ``reconciler_unhealthy_restart_seconds`` — at which point the worker
    livenessProbe (``python -m app.healthcheck reconciler``) restarts the pod
    as a true last resort for an otherwise-unrecoverable wedge.
    """
    path = settings.reconciler_heartbeat_path
    if not path:
        return
    try:
        with open(path, "w") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        logger.debug("Failed to write reconciler heartbeat to %s", path, exc_info=True)


async def _recover_db_pool() -> None:
    """Dispose the SQLAlchemy connection pool to recover the worker in-process.

    Repeated reconciler cycle failures usually mean the worker's pooled
    connections are wedged — a DB failover left them pointing at the old
    primary, a network blip left half-open sockets, or connections were
    cancelled mid-handshake during a transient event-loop stall. Disposing the
    pool drops every connection so the next checkout builds a fresh one,
    recovering WITHOUT a pod restart. Checked-out connections held by other
    tasks are detached and closed on return, so this is safe to call while the
    engine is shared across the worker. Bounded by ``_POOL_DISPOSE_TIMEOUT`` so
    a dispose that blocks closing dead connections can never wedge the loop.
    """
    from app.core.database import engine

    try:
        await asyncio.wait_for(engine.dispose(), timeout=_POOL_DISPOSE_TIMEOUT)
        logger.warning(
            "Reconciler disposed the DB connection pool to recover from "
            "repeated connection failures; fresh connections will be built on "
            "the next cycle"
        )
    except Exception:
        # Never let recovery itself raise out of the loop — a failed/timed-out
        # dispose just means the next cycle retries against the existing pool.
        logger.error(
            "Reconciler DB pool dispose (in-process recovery) failed or timed "
            "out; continuing",
            exc_info=True,
        )


def _get_semaphore() -> asyncio.Semaphore:
    # Built lazily inside the worker event loop (asyncio.Semaphore binds to the
    # running loop on first acquire).
    #
    # This single semaphore now caps EVERY workload the reconciler drives —
    # assignment dispatch, evaluation runs, and sub-agent tasks — which all used
    # to have their own concurrency budgets. Size it as the SUM of the
    # historical assignment-run and sub-agent-task caps so unifying them onto one
    # loop doesn't starve sub-agent / evaluation throughput behind the assignment
    # limit (matches the "max_concurrent_runs + max_concurrent_sub_agent_tasks"
    # total-capacity rule of thumb documented in core/config.py). The per-run
    # sub-agent fan-out cap (max_concurrent_sub_agent_tasks_per_run) is enforced
    # separately in build_sub_agent_tools.
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_semaphore_capacity())
    return _semaphore


def _semaphore_capacity() -> int:
    """Total concurrent-execution slots in the SHARED reconciler pool.

    Single source of truth for both the shared semaphore size and the shared
    per-cycle claim bound — the SUM of the historical assignment-run and
    sub-agent-task caps (see ``_get_semaphore`` for the rationale). Adapters
    with their own ``concurrency_limit`` run on a dedicated semaphore and do NOT
    draw from this pool.
    """
    return max(
        1, settings.max_concurrent_runs + settings.max_concurrent_sub_agent_tasks
    )


def claims_here(adapter) -> bool:
    """Whether this worker claims *adapter*'s executions (``OrchestrationAdapter.claims_here``)."""
    return getattr(adapter, "claims_here", True) is not False


def claimed_adapters(adapters: list | None = None) -> list[str]:
    """The adapters this worker claims the executions of: what its presence lists."""
    return sorted(adapter.name for adapter in (registered_adapters() if adapters is None else adapters) if claims_here(adapter))


def _adapter_concurrency_limit(adapter) -> int | None:
    """The adapter's DEDICATED concurrency budget, or ``None`` for the shared pool."""
    limit = getattr(adapter, "concurrency_limit", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return None


def _slots_held_back(adapter) -> int:
    """The dedicated slots *adapter* holds back on this worker now (``OrchestrationAdapter.slots_held_back``);
    ``0`` when it says none, or what it says cannot be read."""
    try:
        held = getattr(adapter, "slots_held_back", 0)
    except Exception as exc:  # a reservation, never a reason to claim nothing
        # A warning, said once per interval: read on every claim, a failure lets previews start on the CPUs a
        # render reserved.
        from app.core.log_once import log_once_soon

        name = getattr(adapter, "name", "?")
        log_once_soon(
            logger,
            logging.WARNING,
            f"reconciler:slots_held_back:{name}",
            "Reconciler: the slots %s holds back were not read, none held back: %s",
            name,
            format_exception_message(exc),
            interval=600.0,
        )
        return 0
    return max(0, held) if isinstance(held, int) and not isinstance(held, bool) else 0


def _slots_added(adapter) -> int:
    """The slots *adapter* adds on this worker now above its dedicated budget (``OrchestrationAdapter.slots_added``);
    ``0`` when it says none, or what it says cannot be read."""
    try:
        added = getattr(adapter, "slots_added", 0)
    except Exception as exc:  # a growth, never a reason to claim nothing
        from app.core.log_once import log_once_soon

        name = getattr(adapter, "name", "?")
        log_once_soon(
            logger,
            logging.WARNING,
            f"reconciler:slots_added:{name}",
            "Reconciler: the slots %s adds were not read, none added: %s",
            name,
            format_exception_message(exc),
            interval=600.0,
        )
        return 0
    return max(0, added) if isinstance(added, int) and not isinstance(added, bool) else 0


#: The reconciler cycles of another worker a worker whose work reserves CPUs leaves a due execution of the shared pool
#: to, before it runs it here on what is left.
LEAVE_TO_OTHERS_CYCLES = 3


def _reserved_cpus(adapters: list) -> int:
    """The CPUs the work of *adapters* reserves on this worker now (``OrchestrationAdapter.cpus_reserved``);
    what one says that cannot be read reserves none."""
    total = 0
    for adapter in adapters:
        try:
            held = getattr(adapter, "cpus_reserved", 0)
        except Exception as exc:  # a reservation, never a reason to claim nothing
            from app.core.log_once import log_once_soon

            name = getattr(adapter, "name", "?")
            log_once_soon(
                logger,
                logging.WARNING,
                f"reconciler:cpus_reserved:{name}",
                "Reconciler: the CPUs %s reserves were not read, none counted: %s",
                name,
                format_exception_message(exc),
                interval=600.0,
            )
            continue
        if isinstance(held, int) and not isinstance(held, bool) and held > 0:
            total += held
    return total


def _presence_extra(adapters: list) -> dict:
    """What this worker's presence record says beside who it is: the CPUs its work reserves, the executions it
    runs by adapter, and what each adapter adds (``OrchestrationAdapter.presence``)."""
    found: dict = {
        "reserved_cpus": _reserved_cpus(adapters),
        "running": {name: count for name, count in sorted(_inflight_by_adapter.items()) if count > 0},
    }
    for adapter in adapters:
        try:
            found.update(adapter.presence() or {})
        except Exception as exc:
            logger.debug("Adapter %s presence not read: %s", getattr(adapter, "name", "?"), format_exception_message(exc))
    return found


def _leave_seconds(adapter, reserved: int, present: list[dict] | None, worker_id: str) -> float:
    """How long a due execution of *adapter* waits for another worker before this one claims it: an adapter
    of the shared pool (assignment runs, sub-agent tasks, evaluations) while this worker's work reserves CPUs and a
    running worker that claims *adapter* reserves none - :data:`LEAVE_TO_OTHERS_CYCLES` of its cycles -; ``0``
    otherwise, a worker of an earlier release counted as reserving none."""
    if reserved <= 0 or not present or _adapter_concurrency_limit(adapter) is not None:
        return 0.0
    for record in present:
        if (
            str(record.get("worker_id") or "") != worker_id
            and record.get("state") == worker_presence.STATE_RUNNING
            and worker_presence.claims(record, adapter.name)
            and not worker_presence.reserved_cpus(record)
        ):
            return LEAVE_TO_OTHERS_CYCLES * max(1.0, float(settings.reconciler_interval_seconds))
    return 0.0


def _get_adapter_semaphore(adapter_name: str, limit: int) -> asyncio.Semaphore:
    """Dedicated semaphore for an adapter with its own concurrency budget.

    Built lazily inside the worker event loop (like ``_get_semaphore``) and
    cached per adapter name, sized to ``limit`` at first use; only
    :func:`_grow_adapter_semaphore` resizes it afterwards.
    """
    sem = _adapter_semaphores.get(adapter_name)
    if sem is None:
        sem = asyncio.Semaphore(max(1, limit))
        _adapter_semaphores[adapter_name] = sem
        _adapter_semaphore_sizes[adapter_name] = max(1, limit)
    return sem


def _grow_adapter_semaphore(adapter) -> None:
    """Grow *adapter*'s dedicated semaphore to its budget plus the slots it adds now, before a claim into
    them, so an execution claimed into an added slot starts at once. Never shrunk: when the addition falls, the
    executions running keep their slots and the claim bound claims none until the adapter is back under its slots."""
    limit = _adapter_concurrency_limit(adapter)
    if limit is None:
        return
    wanted = limit + _slots_added(adapter)
    sem = _get_adapter_semaphore(adapter.name, wanted)
    size = _adapter_semaphore_sizes.get(adapter.name, wanted)
    for _ in range(wanted - size):
        sem.release()
    _adapter_semaphore_sizes[adapter.name] = max(size, wanted)


def _semaphore_for_adapter(adapter_name: str) -> asyncio.Semaphore:
    """Return the concurrency semaphore an execution of ``adapter_name`` acquires.

    A dedicated semaphore for adapters that declare a positive
    ``concurrency_limit``; the shared pool otherwise (also the safe fallback for
    an unregistered adapter, which can't happen for a claimed row but keeps this
    total).
    """
    if adapter_name == DELEGATION_RESERVE_SLOT:
        return _get_adapter_semaphore(DELEGATION_RESERVE_SLOT, max(1, _delegation_reserve_size()))
    try:
        adapter = get_adapter(adapter_name)
    except AdapterNotFoundError:
        return _get_semaphore()
    limit = _adapter_concurrency_limit(adapter)
    if limit is not None:
        return _get_adapter_semaphore(adapter_name, limit)
    return _get_semaphore()


def _available_slots_for(adapter, adapters: list) -> int:
    """Free execution slots ``adapter`` may claim into THIS cycle.

    Adapters with a dedicated ``concurrency_limit`` are bounded by their own
    budget (limit minus their in-flight). The rest share the reconciler pool:
    ``_semaphore_capacity()`` minus the SHARED in-flight, where shared in-flight
    is the total in-flight minus every dedicated adapter's in-flight (dedicated
    executions run on their own semaphore and must not shrink the shared pool).
    Recomputed per adapter so rows spawned earlier in the same cycle count. A dedicated budget is less the slots the
    adapter holds back on this worker now (``slots_held_back``), plus those it adds now (``slots_added``).
    """
    limit = _adapter_concurrency_limit(adapter)
    if limit is not None:
        own = limit + _slots_added(adapter) - _slots_held_back(adapter)
        return max(0, own - _inflight_by_adapter.get(adapter.name, 0))
    dedicated_inflight = sum(
        _inflight_by_adapter.get(a.name, 0)
        for a in adapters
        if _adapter_concurrency_limit(a) is not None
    ) + _inflight_by_adapter.get(DELEGATION_RESERVE_SLOT, 0)
    shared_inflight = max(0, len(_execution_tasks) - dedicated_inflight)
    return max(0, _semaphore_capacity() - shared_inflight)


#: The slot name of the delegation reserve: slots above the shared pool that only a delegated child whose parent
#: run is still running may take (an adapter's ``deal_reserved_claims``), so parents waiting on their own children
#: never deadlock a full pool.
DELEGATION_RESERVE_SLOT = "delegation_reserve"


def _delegation_reserve_size() -> int:
    """The slots of the delegation reserve on this worker (``delegation_reserve_slots``); 0 when off."""
    size = getattr(settings, "delegation_reserve_slots", 0)
    return max(0, int(size)) if isinstance(size, int) and not isinstance(size, bool) else 0


def _reserve_slots_free() -> int:
    """The delegation reserve's free slots on this worker now."""
    return max(0, _delegation_reserve_size() - _inflight_by_adapter.get(DELEGATION_RESERVE_SLOT, 0))


async def _claim_into_reserve(adapter: Any, worker_id: str, batch: int) -> list[uuid.UUID]:
    """With the shared pool full, claim the due executions of *adapter* its ``deal_reserved_claims`` names
    (delegated children of a live parent) into the delegation reserve; the executions claimed.

    The parent holds its shared slot while it waits for its child: without the reserve, N parents filling the
    pool and each waiting on one child would wait until a deadline. Each child holds a reserve slot until it ends.
    """
    dealer = getattr(adapter, "deal_reserved_claims", None)
    free = _reserve_slots_free()
    if not callable(dealer) or free <= 0:
        return []
    async with async_session() as db:
        claimed = await engine.claim_due(
            db,
            adapter=adapter.name,
            worker_id=worker_id,
            limit=min(batch, free),
            lease_seconds=_lease_seconds_of(adapter),
            deal=dealer,
            slot=DELEGATION_RESERVE_SLOT,
        )
        claimed_ids = [row.id for row in claimed]
        await db.commit()
    for execution_id in claimed_ids:
        logger.info(
            "Execution %s of %s runs in the delegation reserve: its parent run waits for it", execution_id, adapter.name
        )
        _spawn_execution_task(execution_id, adapter.name, worker_id, slot=DELEGATION_RESERVE_SLOT)
    return claimed_ids


def _execution_timeout(execution: OrchestrationExecution) -> float:
    """Per-task wall-clock budget, derived from the execution's deadline."""
    if execution.deadline_at is not None:
        remaining = (execution.deadline_at - datetime.now(UTC)).total_seconds()
        return remaining if remaining > 0 else 1.0
    return float(settings.worker_job_timeout)


#: The timeout of each execution this process runs, by execution id: what :func:`_follow_deadline` pushes out.
_deadline_scopes: dict[uuid.UUID, asyncio.Timeout] = {}


def _follow_deadline(execution_id: uuid.UUID, deadline_at: datetime | None) -> None:
    """Push the running task's timeout out to the execution's deadline when the execution moved it later.

    An execution that pushed its own deadline out (``engine.extend_deadline``) keeps running past the one its task
    was started with; a deadline never comes in this way.
    """
    scope = _deadline_scopes.get(execution_id)
    if scope is None or deadline_at is None:
        return
    when = scope.when()
    target = asyncio.get_running_loop().time() + max(1.0, (deadline_at - datetime.now(UTC)).total_seconds())
    if when is not None and target > when + 1.0:
        scope.reschedule(target)


#: :func:`_follow_deadline` for an adapter that moved its own deadline out at once, not at the next heartbeat.
follow_deadline = _follow_deadline


async def reconciler_loop(draining: asyncio.Event | None = None) -> None:
    """Run the reconciler until cancelled.

    *draining* is set once a stop is asked (the SAQ worker's stop event):
    from then on the reconciler claims nothing new - no execution, no
    outbox event - and lets what runs finish, until the stop itself
    (:func:`stop_reconciler`) hands back what is still running.
    """
    global _stop_event, _draining
    _stop_event = asyncio.Event()
    _draining = draining
    register_builtin_adapters()
    worker_id = _make_worker_id()
    logger.info(
        "Orchestration reconciler started (worker_id=%s, interval=%.1fs)",
        worker_id,
        settings.reconciler_interval_seconds,
    )

    pubsub, pubsub_client = await _subscribe_wake()
    _write_heartbeat()  # mark alive immediately so the probe passes on boot
    consecutive_failures = 0
    first_failure_at: float | None = None
    unhealthy_logged = False
    try:
        while not _stop_event.is_set():
            try:
                await _run_cycle(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                if first_failure_at is None:
                    first_failure_at = time.monotonic()
                failing_for = time.monotonic() - first_failure_at
                logger.error(
                    "Reconciler cycle error (%d consecutive, failing for %.0fs)",
                    consecutive_failures,
                    failing_for,
                    exc_info=True,
                )
                # 1) Self-heal in-process: every N consecutive failures, drop
                #    the (likely wedged) DB connection pool so the next cycle
                #    builds fresh connections — recovering from a failover /
                #    half-open sockets / a transient stall with NO pod restart.
                recover_every = max(
                    1, settings.reconciler_pool_recovery_after_failures
                )
                if consecutive_failures % recover_every == 0:
                    await _recover_db_pool()
                # 2) Keep the heartbeat fresh while actively retrying +
                #    recovering, so a transient DB outage does not cause restart
                #    churn (restarting mid-outage does not help — the worker
                #    self-heals when the DB returns). Only a SUSTAINED,
                #    unrecoverable streak is allowed to go stale so the liveness
                #    probe restarts the worker as a true last resort.
                if failing_for < settings.reconciler_unhealthy_restart_seconds:
                    _write_heartbeat()
                elif not unhealthy_logged:
                    # Log the "give up -> let the liveness probe restart"
                    # transition ONCE per streak. The per-cycle "Reconciler
                    # cycle error" above already records the streak + duration
                    # (with traceback) on every failing cycle, so repeating this
                    # on each subsequent cycle would only double the log volume
                    # during an extended outage without adding signal. After
                    # this we simply leave the heartbeat stale as intended.
                    unhealthy_logged = True
                    logger.error(
                        "Reconciler unable to complete a cycle for %.0fs "
                        "(> %ds); leaving the heartbeat stale so the liveness "
                        "probe can restart the worker as a last resort",
                        failing_for,
                        settings.reconciler_unhealthy_restart_seconds,
                    )
            else:
                if consecutive_failures:
                    logger.info(
                        "Reconciler recovered in-process after %d consecutive "
                        "failed cycle(s)",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                first_failure_at = None
                unhealthy_logged = False
                _write_heartbeat()
            await _sleep_or_wake(pubsub)
    except asyncio.CancelledError:
        logger.info("Orchestration reconciler stopping")
        raise
    finally:
        from app.core.redis import close_pubsub

        await close_pubsub(pubsub, pubsub_client)
        # Still stopping, said again: its presence must not lapse during the waits below (a lapsed worker reads as
        # gone, and its work would be taken back while it unwinds).
        adapters_here = registered_adapters()
        await run_to_completion(
            worker_presence.publish(
                worker_id,
                state=worker_presence.STATE_DRAINING,
                signal=stop_signal(),
                force=True,
                claims=claimed_adapters(adapters_here),
                extra=lambda: _presence_extra(adapters_here),
            )
        )
        # Before its last word: a worker that hears it takes the work back at once, so
        # nothing of this worker may still write to it then. One deadline for both waits, so
        # the same tasks are not waited on twice past the shutdown headroom.
        stop_by = time.monotonic() + LEASE_LOSS_STOP_WAIT_SECONDS
        await _stop_lease_loss_work(timeout=LEASE_LOSS_STOP_WAIT_SECONDS)
        # Let in-flight execution tasks finish their commit on shutdown.
        if _execution_tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(_execution_tasks, timeout=max(0.0, stop_by - time.monotonic()))
        # Its last word: stopped, and by what - the next worker says it of what it resumes.
        await run_to_completion(
            worker_presence.publish(worker_id, state=worker_presence.STATE_STOPPED, signal=stop_signal())
        )


async def _stop_lease_loss_work(timeout: float = LEASE_LOSS_STOP_WAIT_SECONDS) -> None:
    """Stop the executions still running here whose adapter stops on a lost lease, before the worker says it stopped.

    The worker's last word (``STATE_STOPPED``) makes it gone (``worker_presence.is_gone``): a running worker
    takes its executions back within a cycle, and a resumable adapter's next attempt redoes the work in
    flight. An execution still writing here then would write beside it - an evaluation entry stored twice,
    a run flipped back under its new worker. Each is told it lost its lease (its cancel event, the stop its
    adapter already honours, writing nothing) and given up to *timeout* seconds to unwind.
    """
    running = list(_lease_loss_events.values())
    if not running:
        return
    for event, _task in running:
        event.set()
    logger.warning(
        "Worker stopping: %d execution(s) still running told to stop before it says it stopped",
        len(running),
    )
    told = {task for _event, task in running if task is not None and not task.done()}
    if not told:
        return
    with contextlib.suppress(Exception):
        _done, still = await asyncio.wait(told, timeout=max(0.0, timeout))
        if still:
            # Their writes are fenced on the lease (an evaluation's entries), but the window is worth seeing.
            logger.warning(
                "Worker stopping: %d execution(s) told to stop still running after %.0f s",
                len(still),
                timeout,
            )


def stop_reconciler() -> None:
    """Signal the loop to exit after the current cycle (best-effort)."""
    if _stop_event is not None:
        _stop_event.set()


def note_stop_signal(name: str) -> None:
    """Record the signal that asked this worker to stop (``SIGTERM``): its presence says it."""
    global _stop_signal
    _stop_signal = name
    _note_stop_asked()


def stop_signal() -> str:
    """The signal that asked this worker to stop, ``a stop request`` when none was seen."""
    return _stop_signal or "a stop request"


def _note_stop_asked() -> None:
    global _stop_asked_at
    if _stop_asked_at is None:
        _stop_asked_at = time.monotonic()


def stop_asked_at() -> float | None:
    """When (``time.monotonic``) a stop was first asked of this worker - its signal, or its drain first seen."""
    return _stop_asked_at


def _is_draining() -> bool:
    if _draining is not None and _draining.is_set():
        _note_stop_asked()
        return True
    return False


def worker_stopping() -> bool:
    """Whether this worker was asked to stop: its stop signal seen, or its drain."""
    return _is_draining() or _stop_asked_at is not None


async def _subscribe_wake():
    """The wake channel's subscription and its client, or ``(None, None)``: the loop then polls only."""
    from app.core.redis import open_pubsub, redis_pubsub_client

    try:
        client = await redis_pubsub_client()
        return await open_pubsub(client, engine.ORCHESTRATION_WAKE_CHANNEL), client
    except Exception:
        logger.warning(
            "Reconciler wake channel unavailable — falling back to polling only",
            exc_info=True,
        )
        return None, None


# The most buffered wakes one cycle consumes: the rest of a burst are the
# same nudge, and a cycle serves them all.
_MAX_COALESCED_WAKES = 1000


async def _sleep_or_wake(pubsub) -> None:
    """Sleep for the poll interval, or return early on a wake nudge.

    Every wake already buffered is consumed with the first: a burst of N
    nudges costs one cycle, not N back-to-back cycles.
    """
    interval = max(0.1, settings.reconciler_interval_seconds)
    if pubsub is None:
        await asyncio.sleep(interval)
        return
    try:
        # Returns the message immediately on a wake, else None after `interval`.
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        await asyncio.sleep(interval)
        return
    if message is None:
        return
    with contextlib.suppress(Exception):
        for _ in range(_MAX_COALESCED_WAKES):
            if await pubsub.get_message(ignore_subscribe_messages=True, timeout=0) is None:
                break


def resumable_adapters(adapters: list | None = None) -> list[str]:
    """The adapters whose work a worker gone hands to the next one at once, first in the queue."""
    return [
        adapter.name
        for adapter in (registered_adapters() if adapters is None else adapters)
        if getattr(adapter, "resume_when_worker_gone", False)
    ]


def attempt_bound_of(adapters: list | None = None) -> Callable[[str, int], int]:
    """(adapter name, ``max_attempts``) -> the bound that adapter runs such a row under now.

    What the engine's take-backs read before their exhaustion check
    (``OrchestrationAdapter.raised_attempt_bound``): an adapter not registered
    here, or whose hook raises, keeps the row's own bound.
    """
    by_name = {adapter.name: adapter for adapter in (registered_adapters() if adapters is None else adapters)}

    def bound_of(name: str, max_attempts: int) -> int:
        hook = getattr(by_name.get(name), "raised_attempt_bound", None)
        if hook is None:
            return max_attempts
        try:
            return hook(max_attempts)
        except Exception:  # A hook that fails must not stop the sweep: the row keeps its bound.
            logger.exception("raised_attempt_bound of adapter %s failed", name)
            return max_attempts

    return bound_of


def silent_loss_given_back_of(adapters: list | None = None) -> Callable[[str, dict], str | None]:
    """(adapter name, gone holder's last record) -> how a holder gone without a stop request gives the attempt
    back, ``None`` when it spends it: the adapter keeps its progress across workers
    (``OrchestrationAdapter.gives_back_silent_losses``) and the record shows no sign of an out-of-memory kill
    (``worker_presence.out_of_memory_sign``); ``engine.LOSS_ROLLOUT_KILL`` when its worker ran an older release
    than this one (``worker_presence.rollout_kill``), else ``engine.LOSS_SILENT``."""
    giving = {
        adapter.name
        for adapter in (registered_adapters() if adapters is None else adapters)
        if getattr(adapter, "gives_back_silent_losses", False)
    }

    def given_back(name: str, record: dict) -> str | None:
        if name not in giving:
            return None
        try:
            if worker_presence.out_of_memory_sign(record):
                return None
            return engine.LOSS_ROLLOUT_KILL if worker_presence.rollout_kill(record) else engine.LOSS_SILENT
        except Exception:  # Unreadable: the attempt is spent, as before.
            logger.exception("out_of_memory_sign failed for adapter %s", name)
            return None

    return given_back


def waits_for_lease_of(adapters: list | None = None) -> Callable[[str, dict], bool]:
    """(adapter name, gone holder's last record) -> whether its work waits for the lease before it is taken back.

    For an adapter whose work stops on a lost lease (``stop_on_lease_loss``: a replaced attempt must write
    nothing): its holder told that work to stop before it said it stopped only when its record says so
    (``worker_presence.STOPS_LEASE_LOSS_WORK``) and it did say it (``stopped``). A worker of an older release, or
    one gone without that word (its presence lapsed in a stall, a crash), may still be writing, so its work waits
    for the lease as it always did.
    """
    stopping_on_loss = {
        adapter.name
        for adapter in (registered_adapters() if adapters is None else adapters)
        if getattr(adapter, "stop_on_lease_loss", False)
    }

    def waits(name: str, record: dict) -> bool:
        if name not in stopping_on_loss:
            return False
        return not (
            record.get(worker_presence.STOPS_LEASE_LOSS_WORK) is True
            and record.get("state") == worker_presence.STATE_STOPPED
            and not record.get("lapsed")
        )

    return waits


async def _reclaim_from_gone_workers(
    db,
    worker_id: str,
    resumable: list[str],
    batch: int,
    bound_of: Callable[[str, int], int] | None = None,
    now: datetime | None = None,
) -> list[OrchestrationExecution]:
    """Take back the executions of *resumable* adapters a worker that is gone still leases; the dead-lettered.

    Expired leases included: run before the expired-lease sweep, so a
    worker that said it was stopping, or that a deploy replaced, is taken back
    on its last word even when its lease ran out before this worker started.
    """
    owners = await engine.leased_owners(db, adapters=resumable, include_expired=True)
    if not owners:
        return []
    gone = await worker_presence.gone(owners, me=worker_id)
    if not gone:
        return []
    return await engine.reclaim_from_gone_owners(
        db,
        gone,
        adapters=resumable,
        limit=batch,
        reason_of=worker_presence.stop_reason,
        bound_of=bound_of,
        silent_loss_given_back=silent_loss_given_back_of(),
        waits_for_lease=waits_for_lease_of(),
        now=now,
    )


def _deal_of(adapter) -> dict:
    """``{"deal": ...}`` for an adapter that orders its own claims (``deal_claims``), else nothing: its queue is
    claimed oldest ``run_at`` first."""
    dealer = getattr(adapter, "deal_claims", None)
    return {"deal": dealer} if callable(dealer) else {}


async def _lend_idle_slots(adapter, adapters: list, worker_id: str, available: int, batch: int) -> list[uuid.UUID]:
    """Claim the due work of the lane *adapter* lends its idle slots to, into those slots; the executions claimed.

    An adapter naming another in ``lends_idle_slots_to`` (the Video Studio render lane lends to its preview lane)
    has its *available* slots on this worker run that lane's work - in that lane's own order - while its own
    lane is idle here: nothing of its own claimed this cycle, nothing of its own in flight. Only when the other
    lane's own slots are all taken: a lane with room claims for itself. A lent execution holds the lending
    adapter's slot until it ends - the lending lane's next job waits for it, never longer -, or until the lending
    adapter takes it back (:func:`_take_back_lent_slots`); its execution records whose slot it holds
    (``engine.LENT_SLOT_PAYLOAD_KEY``).
    """
    borrower_name = getattr(adapter, "lends_idle_slots_to", None)
    if not borrower_name or available <= 0 or _inflight_by_adapter.get(adapter.name, 0) > 0:
        return []
    borrower = next((candidate for candidate in adapters if candidate.name == borrower_name), None)
    if borrower is None or not claims_here(borrower) or _available_slots_for(borrower, adapters) > 0:
        return []
    async with async_session() as db:
        claimed = await engine.claim_due(
            db,
            adapter=borrower.name,
            worker_id=worker_id,
            limit=min(batch, available),
            lease_seconds=_lease_seconds_of(borrower),
            slot=adapter.name,
            **_deal_of(borrower),
        )
        claimed_ids = [row.id for row in claimed]
        await db.commit()
    for execution_id in claimed_ids:
        logger.info("Execution %s of %s runs in an idle slot of %s", execution_id, borrower.name, adapter.name)
        _spawn_execution_task(execution_id, borrower.name, worker_id, slot=adapter.name)
    return claimed_ids


async def _take_back_lent_slots(adapter) -> int:
    """Ask the executions in *adapter*'s lent slots on this worker to give them back; how many were asked.

    One per execution of *adapter* due in a state it takes its slots back for (``takes_back_lent_slots_for``: a
    Video Studio render a stopped worker left) that has waited :data:`_TAKE_BACK_AFTER_SECONDS` - a worker with a
    free slot claims it first -, less those already asked; the newest lent first, the one that drew least. The
    execution asked hands itself back (``AdapterContext.give_back``), and the slot it frees is the next this
    adapter claims into.
    """
    states = tuple(getattr(adapter, "takes_back_lent_slots_for", None) or ())
    lent = [event for lender, event in reversed(list(_lent.values())) if lender == adapter.name]
    if not states or not lent:
        return 0
    async with async_session() as db:
        waiting = await engine.count_waiting(
            db, adapter=adapter.name, adapter_states=states, waited_seconds=_TAKE_BACK_AFTER_SECONDS
        )
    asked = sum(1 for event in lent if event.is_set())
    fresh = [event for event in lent if not event.is_set()][: max(0, waiting - asked)]
    for event in fresh:
        event.set()
    if fresh:
        logger.info("%d slot(s) %s lent taken back for %d job(s) of its own waiting", len(fresh), adapter.name, waiting)
    return len(fresh)


def _kept_states(adapter) -> tuple[str, ...]:
    """The states an execution of *adapter* waits in with the slot it held (``keeps_slot_through``)."""
    states = getattr(adapter, "keeps_slot_through", ())
    return tuple(states) if isinstance(states, tuple | list) else ()


async def _slots_kept_elsewhere(adapter, worker_id: str) -> int:
    """How many of this worker's free slots of *adapter* it keeps for the executions in flight on a worker asked to
    stop: each comes back unfinished and resumes first, on the slot of a running worker - one a running worker
    claiming *adapter*, in turn, oldest slot first. ``0`` when the workers cannot be read.

    The holders of the adapter's leases are read first (one query, their records in one ``MGET``): the workers
    present are listed only while one of them drains, a deploy's minutes.
    """
    try:
        return await _count_slots_kept_elsewhere(adapter, worker_id)
    except Exception as exc:  # a reservation, never a reason to claim nothing
        logger.debug("Reconciler: the slots kept for stopping workers were not read: %s", exc)
        return 0


async def _count_slots_kept_elsewhere(adapter, worker_id: str) -> int:
    async with async_session() as db:
        owners = [owner for owner in await engine.leased_owners(db, adapters=[adapter.name]) if owner != worker_id]
    if not owners:
        return 0
    found = await worker_presence.records(owners) or {}
    draining = [
        owner
        for owner, record in found.items()
        if record and not record.get("lapsed") and record.get("state") == worker_presence.STATE_DRAINING
    ]
    if not draining:
        return 0
    present = await worker_presence.present() or []
    claimers = {
        str(record["worker_id"]): record.get("version")
        for record in present
        if record.get("state") == worker_presence.STATE_RUNNING
        and record.get("worker_id")
        and worker_presence.claims(record, adapter.name)
    }
    claimers[worker_id] = settings.app_version
    # The work of a draining worker goes to the incoming release first: a running worker of the draining
    # worker's own release is stopped by the same rolling update next. None of another release up: any running one.
    leaving = {found[owner].get("version") for owner in draining} - {None, ""}
    incoming = {owner for owner, version in claimers.items() if isinstance(version, str) and version and version not in leaving}
    running = sorted(incoming if leaving and incoming else claimers)
    if worker_id not in running:
        return 0
    async with async_session() as db:
        kept = await engine.running_on(db, adapter=adapter.name, owners=draining)
    return sum(1 for index in range(len(kept)) if running[index % len(running)] == worker_id)


async def _claimable_slots(adapter, worker_id: str, available: int) -> int:
    """*available* less the slots this worker keeps for the work in flight on stopping workers: never less than
    the executions waiting for the slot they held, which come first in the queue."""
    kept = await _slots_kept_elsewhere(adapter, worker_id)
    if not kept:
        return available
    async with async_session() as db:
        waiting = await engine.waiting_for_slot(db, adapter=adapter.name, worker_id=worker_id, states=_kept_states(adapter))
    return max(available - kept, min(available, len(waiting)))


async def _give_back_slot(adapter, worker_id: str) -> uuid.UUID | None:
    """With no slot of *adapter* free here, the execution that took its slot last gives it back to the one waiting
    with the oldest slot (``OrchestrationAdapter.keeps_slot_through``) - handed to this worker first, so that no
    other worker gives one back for it. The execution giving its slot back, or ``None``."""
    holders = [
        (holder.since, execution_id, holder)
        for execution_id, holder in _slot_holders.items()
        if holder.adapter == adapter.name and not holder.give_back.is_set()
    ]
    if not holders:
        return None
    async with async_session() as db:
        waiting = await engine.waiting_for_slot(
            db,
            adapter=adapter.name,
            worker_id=worker_id,
            states=_kept_states(adapter),
            waited_seconds=GIVE_BACK_AFTER_SECONDS,
        )
        if not waiting:
            return None
        since = engine.slot_since(waiting[0])
        younger = [item for item in holders if since is not None and item[0] > since]
        if not younger:
            return None
        _since, execution_id, holder = max(younger, key=lambda item: item[0])
        if not await engine.hand_to(db, execution_id=waiting[0].id, worker_id=worker_id, seconds=GIVE_BACK_HOLD_SECONDS):
            await db.rollback()
            return None
        await db.commit()
    logger.warning(
        "Execution %s of %s gives its slot back to execution %s, which held one since %s and lost it to a worker that "
        "stopped or went: it goes back to the queue in its place, %s takes the slot",
        execution_id,
        adapter.name,
        waiting[0].id,
        since.isoformat() if since else "?",
        waiting[0].id,
    )
    holder.give_back.set()
    return execution_id


async def _run_cycle(worker_id: str) -> None:
    batch = settings.orchestration_claim_batch
    adapters = registered_adapters()
    draining = _is_draining()
    await worker_presence.publish(
        worker_id,
        state=worker_presence.STATE_DRAINING if draining else worker_presence.STATE_RUNNING,
        claims=claimed_adapters(adapters),
        extra=lambda: _presence_extra(adapters),
    )
    resumable = resumable_adapters(adapters)
    # While its work reserves CPUs (a whole render's pages), the shared pool's work goes to a worker reserving none.
    reserved = 0 if draining else _reserved_cpus(adapters)
    present = await worker_presence.present() if reserved > 0 else None

    # 1 + 2. Global sweeps: the leases of workers gone (on their last word, before any write of this cycle
    # on their rows), dead-letter overdue, then what is left of the expired leases (a holder with no word kept).
    dead_lettered: list[OrchestrationExecution] = []
    # A row queued under an older bound of its adapter is taken back under the new one.
    bound_of = attempt_bound_of(adapters)
    # One clock for both lease sweeps: a lease the gone-worker sweep left to run out (its holder may still
    # write) must not expire in between and be taken by the expired sweep without its holder's word.
    swept_at = datetime.now(UTC)
    async with async_session() as db:
        if resumable:
            dead_lettered.extend(
                await _reclaim_from_gone_workers(db, worker_id, resumable, batch, bound_of, now=swept_at)
            )
        dead_lettered.extend(await engine.sweep_deadlines(db, limit=batch))
        dead_lettered.extend(
            await engine.reclaim_expired_leases(
                db, limit=batch, head_adapters=resumable, bound_of=bound_of, now=swept_at
            )
        )
        await db.commit()
    for execution in dead_lettered:
        await run_dead_letter(execution, worker_id)

    # 3 + 4. Per-adapter claim + periodic sweep. A draining worker claims nothing new.
    # A worker of a release a rollout is replacing leaves the resumable work to the incoming release: claimed
    # here, it would be killed with this worker by the same rollout and lost once more (#12386).
    outgoing_from: list[dict] | None = None
    for adapter in adapters:
        leaving = False
        if not draining and adapter.name in resumable and claims_here(adapter):
            if outgoing_from is None:
                outgoing_from = (present if present is not None else await worker_presence.present()) or []
            leaving = worker_presence.outgoing(outgoing_from, adapter.name)
        # Only lease what we can reasonably start soon: bound the per-cycle claim
        # by this adapter's free execution slots. Adapters with a dedicated
        # ``concurrency_limit`` (e.g. autonomous_pentest) are bounded by their
        # OWN budget; the rest share the reconciler pool. Each claimed row
        # immediately gets a lease + heartbeat, so grabbing a full batch we can't
        # start would hold leases away from idle replicas and add latency.
        # Recomputed per adapter so it accounts for rows just spawned earlier in
        # this cycle. Outbox/periodic work doesn't consume execution slots, so it
        # still runs below.
        available = 0 if draining or leaving or not claims_here(adapter) else _available_slots_for(adapter, adapters)
        kept_states = _kept_states(adapter)
        if kept_states and available > 0:
            # The slots of the work in flight on a worker asked to stop are kept for it.
            available = await _claimable_slots(adapter, worker_id, available)
        if available > 0:
            # The slots it adds now get their permits before an execution is claimed into them.
            _grow_adapter_semaphore(adapter)
            ripe = _leave_seconds(adapter, reserved, present, worker_id)
            async with async_session() as db:
                claimed = await engine.claim_due(
                    db,
                    adapter=adapter.name,
                    worker_id=worker_id,
                    limit=min(batch, available),
                    lease_seconds=_lease_seconds_of(adapter),
                    **_deal_of(adapter),
                    **({"keep_slot": True} if kept_states else {}),
                    **({"ripe_seconds": ripe} if ripe else {}),
                )
                # Detach the bits the tasks need before the session closes.
                claimed_ids = [row.id for row in claimed]
                await db.commit()
            for execution_id in claimed_ids:
                _spawn_execution_task(execution_id, adapter.name, worker_id)
            if not claimed_ids:
                await _lend_idle_slots(adapter, adapters, worker_id, available, batch)
        elif not draining and not leaving and claims_here(adapter):
            if _adapter_concurrency_limit(adapter) is None:
                # The shared pool is full: a delegated child its parent waits for takes a reserve slot.
                try:
                    await _claim_into_reserve(adapter, worker_id, batch)
                except Exception:
                    logger.warning(
                        "Adapter %s: nothing claimed into the delegation reserve", adapter.name, exc_info=True
                    )
            try:
                await _take_back_lent_slots(adapter)
            except Exception:
                logger.warning("Adapter %s: its lent slots were not taken back", adapter.name, exc_info=True)
            lending = any(lender == adapter.name for lender, _event in _lent.values())
            if kept_states and not lending and _available_slots_for(adapter, adapters) == 0:
                # Every slot its own: one taken after an execution that lost its own to a stopping worker goes back.
                try:
                    await _give_back_slot(adapter, worker_id)
                except Exception:
                    logger.warning("Adapter %s: no slot was given back", adapter.name, exc_info=True)

        async with async_session() as db:
            ctx = AdapterContext(db=db, worker_id=worker_id, draining=_draining)
            try:
                await adapter.periodic(ctx)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.error(
                    "Adapter %s periodic() error", adapter.name, exc_info=True
                )

    # 5. Drive pending outbox events - none while draining: an event resumes work, which the next worker does.
    if not draining:
        await _drive_outbox(worker_id)


async def run_dead_letter(
    execution: OrchestrationExecution, worker_id: str
) -> None:
    """Drive one dead-lettered execution's ``adapter.on_dead_letter`` cleanup.

    Resilient by design: each cleanup runs in its own session.  Transient
    failures (DB hiccup, pool exhaustion, brief failover) are retried up to
    ``_DEAD_LETTER_MAX_RETRIES`` times with exponential backoff before giving
    up.  A persistent failure is logged and swallowed so one bad execution
    can't poison a batch's transaction or abort the loop.  The backing run
    remains active and is picked up by ``_cleanup_stale_assignment_runs`` on
    the next scheduler tick (terminal-desync branch).

    Shared by the worker reconciler cycle, the on-demand
    ``/runs/cleanup-stale`` admin endpoint, and the web-process orchestration
    backstop.
    """
    try:
        adapter = get_adapter(execution.adapter)
    except AdapterNotFoundError:
        logger.warning(
            "Dead-lettered execution %s references unknown adapter %r",
            execution.id,
            execution.adapter,
        )
        return
    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(_DEAD_LETTER_MAX_RETRIES + 1):
        attempts = attempt + 1
        try:
            async with async_session() as db:
                ctx = AdapterContext(db=db, worker_id=worker_id)
                await adapter.on_dead_letter(ctx, execution)
                await db.commit()
            return  # success — exit the retry loop
        except asyncio.CancelledError:
            # Cancellation must always propagate immediately (worker
            # shutdown). Redundant on Python >= 3.8 (CancelledError is a
            # BaseException, never caught by ``except Exception``) but kept
            # explicit, matching ``reconciler_loop``.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < _DEAD_LETTER_MAX_RETRIES and _is_transient_dead_letter_error(
                exc
            ):
                delay = _DEAD_LETTER_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "on_dead_letter attempt %d/%d failed for execution %s "
                    "(adapter=%s), retrying in %.1fs: %s",
                    attempt + 1,
                    _DEAD_LETTER_MAX_RETRIES + 1,
                    execution.id,
                    execution.adapter,
                    delay,
                    format_exception_message(exc),
                )
                await asyncio.sleep(delay)
            else:
                # Non-transient failure (corrupt payload, adapter bug): a
                # retry cannot succeed, so don't burn the backoff budget.
                break
    logger.error(
        "on_dead_letter failed for execution %s after %d attempt(s)",
        execution.id,
        attempts,
        exc_info=last_exc,
    )


def _spawn_execution_task(
    execution_id: uuid.UUID, adapter_name: str, worker_id: str, *, slot: str | None = None
) -> None:
    """Run one claimed execution of ``adapter_name`` in a task of its own.

    ``slot`` is the adapter whose concurrency slot it takes (its semaphore and
    its in-flight count): its own by default, the lending adapter's for an
    execution claimed into another adapter's idle slots (:func:`_lend_idle_slots`).
    """
    slot = slot or adapter_name
    if slot != adapter_name:
        _lent[execution_id] = (slot, asyncio.Event())
    task = asyncio.create_task(
        _run_execution(execution_id, adapter_name, worker_id, slot=slot)
    )
    _execution_tasks.add(task)
    _inflight_by_adapter[slot] = _inflight_by_adapter.get(slot, 0) + 1

    def _on_done(
        finished: asyncio.Task,
        _eid: uuid.UUID = execution_id,
        _adapter: str = slot,
    ) -> None:
        # Discard AND observe the result. Without retrieving the exception a
        # crash in _run_execution (e.g. an unexpected error outside its own
        # handlers) is swallowed and only surfaces later as asyncio's
        # unstructured "Task exception was never retrieved" warning.
        _execution_tasks.discard(finished)
        _lent.pop(_eid, None)
        remaining = _inflight_by_adapter.get(_adapter, 0) - 1
        if remaining > 0:
            _inflight_by_adapter[_adapter] = remaining
        else:
            _inflight_by_adapter.pop(_adapter, None)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            logger.error(
                "Execution task for %s (adapter=%s) crashed unexpectedly: %s",
                _eid,
                _adapter,
                exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)


def _writer_scope(adapter, execution_id: uuid.UUID, worker_id: str, execution) -> contextlib.AbstractContextManager:
    """The single-writer check of *execution*'s turn, for an adapter that asks for it; a no-op otherwise.

    One primary-key read of the row per tool round, on a session of its own: the
    turn is *worker_id*'s at attempt ``execution.attempt_count`` until another
    attempt took it over (``orchestration_engine.superseded_by``).
    """
    if not getattr(adapter, "single_writer_turns", False):
        return contextlib.nullcontext()
    attempt = getattr(execution, "attempt_count", None)

    async def _check() -> str | None:
        async with async_session() as db:
            row = (
                await db.execute(
                    select(
                        OrchestrationExecution.state,
                        OrchestrationExecution.lease_owner,
                        OrchestrationExecution.attempt_count,
                    ).where(OrchestrationExecution.id == execution_id)
                )
            ).one_or_none()
        if row is None:
            return engine.superseded_by(
                state=None, lease_owner=None, attempt_count=None, worker_id=worker_id, attempt=attempt
            )
        return engine.superseded_by(
            state=row.state,
            lease_owner=row.lease_owner,
            attempt_count=row.attempt_count,
            worker_id=worker_id,
            attempt=attempt,
        )

    return single_writer(_check)


def _stops_on_lease_loss(adapter_name: str) -> bool:
    """Whether ``adapter_name`` asked to be stopped when its lease is lost.

    ``False`` for an unregistered adapter, which cannot happen for a claimed row
    but keeps this total.
    """
    try:
        return get_adapter(adapter_name).stop_on_lease_loss
    except AdapterNotFoundError:
        return False


async def _run_execution(
    execution_id: uuid.UUID, adapter_name: str, worker_id: str, *, slot: str | None = None
) -> None:
    """Supervise one claimed execution: heartbeat + bounded run + transition.

    ``slot``: the adapter whose concurrency slot it holds (its own by default).
    """
    # ONE cancel event per execution, owned here: "is there a stop
    # signal" and "do you want fast polling" are separate questions. The
    # heartbeat fires it on a lost lease only for an adapter that opted in
    # (``stop_on_lease_loss``), and ``_do_execute`` hands it to the
    # adapter only when something can fire it.
    cancel_event = asyncio.Event()
    # Decided ONCE, here: the heartbeat and ``_do_execute`` must agree on it.
    stop_on_lease_loss = _stops_on_lease_loss(adapter_name)
    if stop_on_lease_loss:
        _lease_loss_events[execution_id] = (cancel_event, asyncio.current_task())
    try:
        lease_seconds = _lease_seconds_of(get_adapter(adapter_name))
    except AdapterNotFoundError:
        lease_seconds = None
    heartbeat = asyncio.create_task(
        _heartbeat(
            execution_id,
            worker_id,
            cancel_event if stop_on_lease_loss else None,
            lease_seconds=lease_seconds,
        )
    )
    try:
        # Heartbeat starts immediately so the lease stays valid even while this
        # task waits for a concurrency slot. Adapters with a dedicated
        # ``concurrency_limit`` acquire their OWN semaphore (isolated budget);
        # the rest share the reconciler pool.
        async with _semaphore_for_adapter(slot or adapter_name):
            await _do_execute(
                execution_id,
                adapter_name,
                worker_id,
                cancel_event,
                stop_on_lease_loss=stop_on_lease_loss,
                slot=slot,
            )
    finally:
        if (_lease_loss_events.get(execution_id) or (None,))[0] is cancel_event:
            del _lease_loss_events[execution_id]
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            # Expected: we just called heartbeat.cancel() above, so draining the
            # task surfaces the cancellation. Nothing to do.
            pass
        except Exception:
            logger.debug(
                "Heartbeat task error on drain for %s", execution_id, exc_info=True
            )
        if adapter_name == "assignment":
            # One of the assignment's slots just freed: the drain refills it
            # now rather than after the poll interval. Never raises
            # and never delays a cancellation: the interval covers a lost wake.
            current = asyncio.current_task()
            if not (current is not None and current.cancelling()):
                with contextlib.suppress(Exception):
                    await engine.notify_orchestration_wake()


async def _do_execute(
    execution_id: uuid.UUID,
    adapter_name: str,
    worker_id: str,
    cancel_event: asyncio.Event,
    *,
    stop_on_lease_loss: bool,
    slot: str | None = None,
) -> None:
    async with async_session() as db:
        execution = await engine.get_execution(db, execution_id)
        if (
            execution is None
            or execution.state != "running"
            or execution.lease_owner != worker_id
        ):
            # Lost the lease (reclaimed/cancelled) before we started — bail.
            logger.debug("Execution %s no longer ours; skipping", execution_id)
            return

        adapter = get_adapter(adapter_name)
        timeout = _execution_timeout(execution)

        # ``cancel_event`` means "stop this execution", with two one-way
        # producers:
        #
        # * the heartbeat, when the LEASE is lost -- only for an adapter that
        #   opted in via ``stop_on_lease_loss``;
        # * the fast watcher below, for adapters that opt in via
        #   ``cancel_poll_seconds``, when the row goes TERMINAL out-of-band
        #   (operator cancel/delete).
        #
        # An adapter with neither gets ``None``: the agent loop races every
        # await against a non-None event (``_race_cancellation``), where
        # ``None`` is an exact passthrough. The lease-loss paths below are
        # scoped to ``stop_on_lease_loss`` too.
        cancel_watcher = _start_cancel_watcher(adapter, execution_id, cancel_event)
        lent = _lent.get(execution_id)
        holder = None
        if lent is None and _kept_states(adapter) and (slot or adapter_name) == adapter_name:
            # It holds its slot to its end: one an execution that lost its own may ask back.
            holder = _SlotHolder(adapter_name, engine.slot_since(execution) or datetime.now(UTC))
        ctx = AdapterContext(
            db=db,
            worker_id=worker_id,
            cancel_event=cancel_event
            if stop_on_lease_loss or cancel_watcher is not None
            else None,
            stopping=_stop_event,
            draining=_draining,
            give_back=lent[1] if lent is not None else holder.give_back if holder is not None else None,
        )

        # Release the read-only transaction (and its pooled connection) opened by
        # the lease re-read above before entering the potentially long-running
        # adapter.execute() (the full agentic loop can run for minutes). Holding
        # an idle transaction across that work pins a connection and shrinks pool
        # capacity under load. This is safe: the lease is kept alive by the
        # separate heartbeat task (its own session), the engine's terminal
        # transitions (complete/fail/await_event) re-acquire a connection and
        # re-read FOR UPDATE with a lease check, and expire_on_commit=False keeps
        # the already-loaded ``execution`` attributes readable without a refresh.
        # Adapters that genuinely need a session during work (e.g. evaluation)
        # transparently start a fresh transaction on their next ctx.db use.
        await db.commit()

        try:
            if holder is not None:
                _slot_holders[execution_id] = holder
            try:
                # Each execution binds whom its AI work is for (LLM provider
                # exclusions); the scope ends that binding with it, so
                # the reconciler never carries one person into the next run.
                async with asyncio.timeout(timeout) as scope:
                    # Followed by the heartbeat when the execution pushes its own deadline out.
                    _deadline_scopes[execution_id] = scope
                    try:
                        with acting_for(None), _writer_scope(adapter, execution_id, worker_id, execution):
                            result = await adapter.execute(ctx, execution)
                    finally:
                        _deadline_scopes.pop(execution_id, None)
            except TurnSuperseded:
                # Another attempt took this execution over while its turn ran:
                # the turn stopped before its next tool round, and the
                # execution is that attempt's. Nothing is written.
                logger.warning(
                    "Execution %s: its turn stopped, another attempt took the execution over; "
                    "no terminal state written",
                    execution_id,
                )
                return
            except TimeoutError:
                # ``asyncio.timeout`` turns a deadline expiring CONCURRENTLY
                # with an event-caused cancellation into a TimeoutError.
                # "Execution timed out" would then name the wrong cause on a
                # row that is no longer ours; the lease predicate blocks that
                # write, but don't rely on it. Take the lease-loss path.
                if stop_on_lease_loss and cancel_event.is_set():
                    logger.info(
                        "Execution %s: deadline expired while stopping; this "
                        "execution is no longer ours, writing nothing",
                        execution_id,
                    )
                    return
                await _safe_terminal(
                    db, execution_id, worker_id, error="Execution timed out", fail=True
                )
                return
            except engine.LeaseLostError:
                logger.debug("Lease lost during execute for %s", execution_id)
                return
            except asyncio.CancelledError:
                # An adapter that does not classify its own cancellation would
                # let a raw CancelledError reach the task; ``_on_done`` then
                # sees ``task.cancelled()`` and logs NOTHING, so the stop reads
                # like a worker shutdown. Log the event-caused case
                # here so every adapter inherits it.
                if stop_on_lease_loss and cancel_event.is_set():
                    logger.info(
                        "Execution %s: adapter %s stopped, this execution is "
                        "no longer ours; no terminal state written",
                        execution_id,
                        adapter_name,
                    )
                    return
                raise
            except Exception as exc:
                logger.error("Adapter %s.execute failed: %s", adapter_name, format_exception_message(exc), exc_info=True)
                await _safe_terminal(
                    db,
                    execution_id,
                    worker_id,
                    error=format_exception_message(exc),
                    fail=True,
                )
                return

            result = await _to_incoming_release(result, adapter_name, worker_id)
            await _apply_result(db, execution_id, worker_id, result)
        finally:
            if holder is not None and _slot_holders.get(execution_id) is holder:
                del _slot_holders[execution_id]
            await _stop_cancel_watcher(cancel_watcher)


async def _to_incoming_release(result, adapter_name: str, worker_id: str):
    """*result*, a hand-back of this stopping worker, held for a running worker of the incoming release.

    A deferral to the head of the queue (``place="head"``) decided while this worker stops, that names no
    worker yet, is handed to the first running worker of another release that claims *adapter_name*'s
    executions (``worker_presence.incoming_worker``) for :data:`HANDBACK_INCOMING_HOLD_SECONDS`, so one run
    is handed back once per rollout, not once per worker of the outgoing release. With no such worker yet it
    is kept from the workers of this release (``not_release``) for :data:`HANDBACK_OUTGOING_HOLD_SECONDS`,
    any worker after. A deferral that already names its worker or its release, any other result, or Redis
    unreadable: *result* unchanged - never what keeps work from being handed back.
    """
    if (
        getattr(result, "kind", None) != "defer"
        or getattr(result, "place", None) != "head"
        or getattr(result, "claim_for", None)
        or getattr(result, "not_release", None)
        or not worker_stopping()
    ):
        return result
    try:
        taker = await worker_presence.incoming_worker(adapter_name, me=worker_id)
    except Exception as exc:
        logger.debug("Incoming worker for %s not read: %s", adapter_name, format_exception_message(exc))
        return result
    if taker:
        return dataclasses.replace(result, claim_for=(taker, HANDBACK_INCOMING_HOLD_SECONDS))
    if not settings.app_version:
        return result
    return dataclasses.replace(result, not_release=(str(settings.app_version), HANDBACK_OUTGOING_HOLD_SECONDS))


def _retry_options(result) -> dict:
    """``engine.fail``'s optional arguments for *result*: the backoff it names,
    if any - otherwise the engine's own exponential backoff applies."""
    if result.backoff_seconds is None:
        return {}
    return {"backoff_seconds": result.backoff_seconds}


def _defer_options(result) -> dict:
    """``engine.defer``'s arguments for a deferral *result*."""
    return {
        "reason": result.error or "Deferred",
        "delay_seconds": result.backoff_seconds or 0.0,
        "adapter_state": result.adapter_state or "deferred",
        "payload_update": result.payload_update,
        "place": result.place,
        **({"claim_for": result.claim_for} if getattr(result, "claim_for", None) else {}),
        **({"not_for": result.not_for} if getattr(result, "not_for", None) else {}),
        **({"not_release": result.not_release} if getattr(result, "not_release", None) else {}),
    }


async def _apply_result(db, execution_id, worker_id, result) -> None:
    dead_lettered: OrchestrationExecution | None = None
    try:
        if result.kind == "completed":
            await engine.complete(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                payload_update=result.payload_update,
            )
        elif result.kind == "await":
            await engine.await_event(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_state=result.adapter_state or "awaiting",
                deadline_at=result.deadline_at,
            )
        elif result.kind == "defer":
            await engine.defer(db, execution_id=execution_id, worker_id=worker_id, **_defer_options(result))
        else:  # failed
            execution = await engine.fail(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                error=result.error or "Adapter reported failure",
                retryable=result.retryable,
                **_retry_options(result),
            )
            if execution.state == "dead_letter":
                dead_lettered = execution
        await db.commit()
    except engine.LeaseLostError:
        await db.rollback()
        logger.debug("Lease lost applying result for %s", execution_id)
        return
    except Exception:
        await db.rollback()
        logger.error("Failed to apply result for %s", execution_id, exc_info=True)
        return
    if result.kind == "defer" and (result.place == "head" or not result.backoff_seconds):
        # Due now - work a stopping worker handed back: every worker's next cycle may take it, not the
        # next job queued.
        with contextlib.suppress(Exception):
            await engine.notify_orchestration_wake()
    # engine.fail() can land an execution in ``dead_letter`` (attempts exhausted
    # or past deadline) without going through the deadline/lease sweeps that
    # normally drive ``on_dead_letter`` — run the adapter cleanup here too so a
    # dead-lettered execution still flips its backing row (SubAgentTask /
    # AssignmentRun / delegation) terminal instead of stranding it on
    # pending/running. on_dead_letter only touches still-active rows, so it is a
    # no-op when the adapter body already recorded the terminal state.
    if dead_lettered is not None:
        await run_dead_letter(dead_lettered, worker_id)


async def _safe_terminal(
    db, execution_id, worker_id, *, error: str, fail: bool
) -> None:
    dead_lettered: OrchestrationExecution | None = None
    try:
        if fail:
            execution = await engine.fail(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                error=error,
                retryable=True,
            )
            if execution.state == "dead_letter":
                dead_lettered = execution
        await db.commit()
    except engine.LeaseLostError:
        await db.rollback()
        return
    except Exception:
        await db.rollback()
        logger.error("Failed to record terminal state for %s", execution_id, exc_info=True)
        return
    # A reconciler-driven timeout / adapter crash that exhausts attempts (or is
    # past the deadline) lands the execution in ``dead_letter`` here rather than
    # via the sweeps, so drive the adapter cleanup so the backing row is failed
    # instead of stranded on pending/running.
    if dead_lettered is not None:
        await run_dead_letter(dead_lettered, worker_id)


def _start_cancel_watcher(
    adapter, execution_id: uuid.UUID, cancel_event: asyncio.Event
) -> asyncio.Task | None:
    """Start the fast terminal-row watcher for an opted-in adapter.

    The caller owns the event because the heartbeat fires it too, for an
    adapter that opted in via ``stop_on_lease_loss``. Returns ``None``
    when the adapter sets no positive ``cancel_poll_seconds``.
    """
    poll = getattr(adapter, "cancel_poll_seconds", None)
    if not isinstance(poll, (int, float)) or poll <= 0:
        return None
    return asyncio.create_task(
        _watch_execution_cancel(execution_id, cancel_event, float(poll))
    )


async def _stop_cancel_watcher(watcher: asyncio.Task | None) -> None:
    """Cancel + drain the fast cancel watcher once execute() has returned."""
    if watcher is None:
        return
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await watcher


async def _watch_execution_cancel(
    execution_id: uuid.UUID, cancel_event: asyncio.Event, poll: float
) -> None:
    """Fire ``cancel_event`` the instant an in-flight execution goes terminal.

    An operator cancel/delete flips the execution ``cancelled`` from the WEB
    process; a burst already running under its lease in the worker would keep
    driving decision cycles until the current one ends. Polling the row every
    ``poll`` seconds and firing the event lets the adapter's ``execute`` (and the
    ``run_agent_chat`` agent loop it drives, which already checks ``cancel_event``
    between iterations) bail promptly. Best-effort: any poll error is swallowed
    and retried; the lease heartbeat's ``LeaseLostError`` is the backstop.
    """
    while not cancel_event.is_set():
        await asyncio.sleep(poll)
        try:
            async with async_session() as db:
                state = (
                    await db.execute(
                        select(OrchestrationExecution.state).where(
                            OrchestrationExecution.id == execution_id
                        )
                    )
                ).scalar_one_or_none()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "cancel-watch poll error for %s", execution_id, exc_info=True
            )
            continue
        if state is None or state in TERMINAL_EXECUTION_STATES:
            cancel_event.set()
            return


def _lease_seconds_of(adapter) -> int | None:
    """The lease *adapter* declares for its executions (``lease_seconds``), or ``None`` for the engine's."""
    own = getattr(adapter, "lease_seconds", None)
    return int(own) if isinstance(own, (int, float)) and own > 0 else None


async def _heartbeat(
    execution_id: uuid.UUID,
    worker_id: str,
    cancel_event: asyncio.Event | None = None,
    *,
    lease_seconds: int | None = None,
) -> None:
    """Keep the lease alive, and stop the work the moment it is no longer ours.

    ``extend_lease`` fails exactly when the row is no longer ours; firing
    ``cancel_event`` then stops an abandoned worker from judging runs its
    replacement is judging, with no extra query. ``cancel_event`` is
    ``None`` for an adapter that did not opt in (``stop_on_lease_loss``):
    it stops beating and lets the work finish.

    ONLY the ``LeaseLostError`` arm fires the event. The generic ``except
    Exception`` arm logs and keeps beating, a deliberate fail-OPEN against the
    constitution's fail-closed default: "could not tell" is not "not
    ours", and stopping on a database blip would strand work nobody has taken.
    Don't simplify it to cancel on any heartbeat failure.
    """
    lease = lease_seconds or settings.orchestration_lease_seconds
    interval = max(1, lease // 3)
    failures = 0
    renewed = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        try:
            # A renewal that hangs (a pool exhausted, a lock) is given up after an
            # interval, so the next one starts on time.
            async with asyncio.timeout(interval):
                async with async_session() as db:
                    deadline_at = await engine.extend_lease(
                        db,
                        execution_id=execution_id,
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
                    await db.commit()
            failures = 0
            renewed = time.monotonic()
            _follow_deadline(execution_id, deadline_at)
        except engine.LeaseLostError:
            # The ONE arm that cancels. ``info``, not ``debug``: an operator
            # asking why a run stopped has to find this.
            #
            # Neutral wording: ``extend_lease`` also raises this when
            # the row is TERMINAL, which is how an operator cancel arrives for
            # an adapter with no fast watcher.
            #
            # ``cancel_event`` is None for an adapter that did not opt in
            # (``stop_on_lease_loss``): stop beating, let the work finish.
            if cancel_event is None:
                logger.debug("Heartbeat: lease lost for %s; stopping", execution_id)
                return
            logger.info(
                "Heartbeat: execution %s is no longer ours (worker %s) — "
                "lease reassigned or execution already terminal; "
                "cancelling in-flight work",
                execution_id,
                worker_id,
            )
            cancel_event.set()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately does NOT cancel and keeps beating: "could not tell"
            # is not "not ours", and a database blip must not strand a run.
            # Fail-OPEN, a deliberate exception to fail-closed.
            # Said, never swallowed: a few of these in a row expire the lease
            # of work that is still running.
            failures += 1
            logger.warning(
                "Heartbeat of execution %s failed (%d in a row, renewed %.0f s ago, "
                "the lease lasts %d s): %s",
                execution_id,
                failures,
                time.monotonic() - renewed,
                lease,
                format_exception_message(exc) or type(exc).__name__,
            )


async def _drive_outbox(worker_id: str) -> None:
    batch = settings.orchestration_claim_batch
    async with async_session() as db:
        events = await engine.claim_outbox_events(
            db, worker_id=worker_id, limit=batch
        )
        event_ids = [(ev.id, ev.execution_id, ev.event_type) for ev in events]
        await db.commit()
    for event_id, _execution_id, _event_type in event_ids:
        await _handle_outbox_event(event_id, worker_id)


async def _handle_outbox_event(event_id: uuid.UUID, worker_id: str) -> None:
    async with async_session() as db:
        event = await engine._locked_get_outbox(db, event_id)
        if event is None or event.status != "pending":
            await db.commit()
            return
        execution = await engine.get_execution(db, event.execution_id)
        if execution is None:
            await engine.mark_outbox_processed(db, event_id=event_id)
            await db.commit()
            return
        try:
            adapter = get_adapter(execution.adapter)
        except AdapterNotFoundError:
            await engine.mark_outbox_failed(
                db, event_id=event_id, error="adapter not registered"
            )
            await db.commit()
            return

        ctx = AdapterContext(db=db, worker_id=worker_id)
        try:
            with acting_for(None):  # as around execute()
                result = await adapter.handle_event(ctx, execution, event)
            if result is not None:
                await _apply_event_result(db, execution.id, result)
            await engine.mark_outbox_processed(db, event_id=event_id)
            await db.commit()
        except engine.ResumeNotReadyError:
            # Benign race: the resume arrived before the execution paused. Back
            # off and retry via the outbox (not an error) — the worker will
            # release its lease shortly and the next delivery will re-arm.
            await db.rollback()
            logger.debug(
                "Outbox event %s not ready (execution still lease-held); retrying",
                event_id,
            )
            async with async_session() as db2:
                await engine.mark_outbox_failed(
                    db2,
                    event_id=event_id,
                    error="resume not ready; execution still lease-held",
                )
                await db2.commit()
        except Exception as exc:
            await db.rollback()
            logger.error(
                "Outbox handler failed for event %s: %s", event_id, format_exception_message(exc), exc_info=True
            )
            async with async_session() as db2:
                await engine.mark_outbox_failed(
                    db2, event_id=event_id, error=format_exception_message(exc)
                )
                await db2.commit()


async def _apply_event_result(db, execution_id, result) -> None:
    """Apply a lease-free terminal/await transition from an outbox handler."""
    if result.kind == "completed":
        await engine.complete(
            db,
            execution_id=execution_id,
            require_lease=False,
            payload_update=result.payload_update,
        )
    elif result.kind == "await":
        await engine.await_event(
            db,
            execution_id=execution_id,
            require_lease=False,
            adapter_state=result.adapter_state or "awaiting",
            deadline_at=result.deadline_at,
        )
    elif result.kind == "defer":
        await engine.defer(db, execution_id=execution_id, require_lease=False, **_defer_options(result))
    else:  # failed
        await engine.fail(
            db,
            execution_id=execution_id,
            require_lease=False,
            error=result.error or "Adapter reported failure",
            retryable=result.retryable,
            **_retry_options(result),
        )
