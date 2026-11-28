# SPDX-License-Identifier: Apache-2.0

"""The CTEM passes, run off the scheduler tick (#2774).

Until #2774 the scheduler tick ran every CTEM pass (case expiry, the recheck of
signals left ``new``, the signal inbox, the paced opening, the case sweep, the
case recompute, attack surface, evidence health, inventory evaluation, the
ticketing sync) between the work-item releases and the work-item inbox. A
signal backlog made one of them take minutes, the tick overran its window and
schedules missed their slots.

They run here instead, in their own background task (:func:`run_loop`, started
by ``main.py``), on their own cadence (:data:`LOOP_SECONDS`) and under their own
Redis lock (:data:`LOCK_KEY`), so one replica runs them at a time and no CTEM
backlog ever delays schedule evaluation. Each pass keeps the cadence slot it
had (``recheck_new_due``, ``paced_pass_due``, ``sweep_due``, ``sync_due``, the
profile cadences) and its intake-paused rule (#2528), opens its own session and
is given a deadline (:data:`PASS_BUDGET_SECONDS`): the batch passes stop at it
row by row and commit what they did; a pass still running past the deadline and
:data:`PASS_GRACE_SECONDS` is cancelled (its open transaction rolls back) and the
next pass runs. The rest waits for the next cycle.

dev-docs/ctem-cases.md section 9bis.3 is the contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Seconds between two cycles of the CTEM passes on one process.
LOOP_SECONDS = 60.0
#: Seconds one pass may work before it stops at its next row and commits.
PASS_BUDGET_SECONDS = 20.0
#: Past its budget, how long a pass is given to commit before it is cancelled.
PASS_GRACE_SECONDS = 10.0
#: The Redis lock that keeps the passes to one replica at a time.
LOCK_KEY = "ctem:background-passes:lock"
#: The lock's lifetime: longer than a whole cycle of cancelled passes, so it never expires under a live holder.
LOCK_SECONDS = 900
#: A cycle longer than this is logged once, naming its slowest pass.
CYCLE_WARNING_SECONDS = 120.0

_UNLOCK_SCRIPT = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

Pass = Callable[[float], Awaitable[Any]]


async def _paused() -> bool:
    from app.services.ctem_signal_purge import intake_paused

    return bool(await intake_paused())


async def _case_expiry(_deadline: float) -> None:
    """#11545: an accepted risk past its expiry returns to needs attention."""
    from app.core.database import async_session
    from app.services.ctem_cases_store import expire_acceptances

    async with async_session() as db:
        expired = await expire_acceptances(db)
        if expired:
            await db.commit()
            logger.info("Returned %d CTEM case(s) whose accepted risk expired", expired)


async def _new_signal_recheck(deadline: float) -> None:
    """#2530 and #2542: the signals left ``new`` past the grace, then the failed software reads, decided again."""
    from app.core.database import async_session
    from app.services import ctem_scope_gate_store

    if not await ctem_scope_gate_store.recheck_new_due():
        return
    async with async_session() as db:
        undecided = await ctem_scope_gate_store.drain_new(db, deadline=deadline)
        if undecided:
            logger.info("CTEM signals left new decided by the scheduled recheck: %s", undecided)
        retried = await ctem_scope_gate_store.recheck_lookup_failed(db, deadline=deadline)
        await db.commit()
        if retried:
            logger.info("CTEM signals decided again after a failed software read: %s", retried)


async def _signal_inbox(deadline: float) -> None:
    """#2570, #2584: the verified deliveries the webhook accepted, recorded."""
    from app.services import ctem_signal_inbox

    drained = await ctem_signal_inbox.drain_within(deadline=deadline)
    if any(drained.values()):
        logger.info("CTEM signal inbox drained: %s", drained)


async def _paced_opening(_deadline: float) -> None:
    """#2570: the paced opening pass, within the per-minute opening budget."""
    from app.services import ctem_case_opening

    if await _paused() or not await ctem_case_opening.paced_pass_due():
        return
    paced = await ctem_case_opening.paced_pass("cyber")
    if paced and (paced.get("opened") or paced.get("joined")):
        logger.info("CTEM paced case opening: %s", paced)


async def _case_sweep(deadline: float) -> None:
    """#857: open the waiting cases, escalate SLA breaches; #1505 profile recheck first; #1390, #2451 after."""
    from app.core.database import async_session
    from app.services import ctem_case_loop, ctem_decision_groups, ctem_scope_gate_store
    from app.services.coordination_retention import effective_coordination_retention
    from app.services.ctem_case_routing import catch_up

    # #2528: no recheck and no case sweep while a purge of every signal runs.
    if await _paused() or not await ctem_case_loop.sweep_due():
        return
    async with async_session() as db:
        # #2726: only the unmatched signals seen within their retention window.
        unmatched_days = (await effective_coordination_retention()).get("ctem_signal_unmatched_days", 0)
        rechecked = await ctem_scope_gate_store.recheck_parked(db, seen_within_days=unmatched_days, deadline=deadline)
        await db.commit()
        if rechecked:
            logger.info("CTEM unmatched signals re-checked after a profile change: %s", rechecked)
        pending = ctem_case_loop.PendingEvents()
        counts = await ctem_case_loop.sweep(db, events=pending, deadline=deadline)
        await db.commit()
        await ctem_case_loop.publish_pending(pending)
        counts["caught_up"] = await catch_up()
        counts["decisions_grouped"] = await ctem_decision_groups.consolidate(db)
        await db.commit()
        if any(counts.values()):
            logger.info("CTEM case sweep: %s", counts)


async def _cases_refresh(_deadline: float) -> None:
    """dev-docs/ctem-cases.md s.4.3: the source recompute on its own interval."""
    from app.core.database import async_session
    from app.services import ctem_cases_refresh

    async with async_session() as db:
        counts = await ctem_cases_refresh.scheduled_pass(db)
        if counts and counts.get("applied"):
            logger.info("CTEM cases scheduled recompute: %s", counts)


async def _attack_surface(_deadline: float) -> None:
    """CTEM-26: the outside lens, opt-in, its own cadence."""
    from app.core.database import async_session
    from app.services import ctem_attack_surface

    # #2528: the pass keeps its cadence slot for after a purge of every signal.
    if await _paused():
        return
    async with async_session() as db:
        counts = await ctem_attack_surface.scheduled_pass(db)
        if counts:
            logger.info("CTEM attack surface pass: %s", counts)


async def _evidence_health(_deadline: float) -> None:
    """CTEM-28: evidence decay and blind spots from OpenCRQ, opt-in, its own cadence."""
    from app.core.database import async_session
    from app.services import ctem_evidence_health

    if await _paused():
        return
    async with async_session() as db:
        counts = await ctem_evidence_health.scheduled_pass(db)
        if counts:
            logger.info("CTEM evidence health pass: %s", counts)


async def _inventory_evaluation(_deadline: float) -> None:
    """#2642: path 6 on the inventory as it stands, daily."""
    from app.core.database import async_session
    from app.services import ctem_inventory_staleness

    if await _paused():
        return
    async with async_session() as db:
        counts = await ctem_inventory_staleness.scheduled_pass(db)
        if counts:
            logger.info("CTEM inventory evaluation: %s", counts)


async def _ticketing_sync(_deadline: float) -> None:
    """#917: the ticket of a mobilized case, its status pulled, the case state pushed."""
    from app.core.config import settings as app_settings
    from app.core.database import async_session
    from app.services import ctem_case_loop, ctem_ticketing

    if not await ctem_ticketing.sync_due():
        return
    async with async_session() as db:
        pending = ctem_case_loop.PendingEvents()
        counts = await ctem_ticketing.sync_cases(db, events=pending, base_url=app_settings.frontend_url)
        await db.commit()
        await ctem_case_loop.publish_pending(pending)
        if any(v for k, v in counts.items() if k != "not_connected"):
            logger.info("CTEM ticketing sync: %s", counts)


#: The passes, in the order the tick ran them before #2774.
PASSES: tuple[tuple[str, Pass], ...] = (
    ("case expiry", _case_expiry),
    ("new-signal recheck", _new_signal_recheck),
    ("signal inbox", _signal_inbox),
    ("paced case opening", _paced_opening),
    ("case sweep", _case_sweep),
    ("cases recompute", _cases_refresh),
    ("attack surface", _attack_surface),
    ("evidence health", _evidence_health),
    ("inventory evaluation", _inventory_evaluation),
    ("ticketing sync", _ticketing_sync),
)


async def run_pass(name: str, call: Pass) -> float:
    """One pass within its deadline; its duration. A pass past the deadline and the grace is cancelled."""
    started = time.monotonic()
    deadline = started + PASS_BUDGET_SECONDS
    try:
        await asyncio.wait_for(call(deadline), timeout=PASS_BUDGET_SECONDS + PASS_GRACE_SECONDS)
    except TimeoutError:
        logger.warning(
            "CTEM pass '%s' cancelled past its %ds budget: the rest waits for the next cycle",
            name,
            int(PASS_BUDGET_SECONDS + PASS_GRACE_SECONDS),
        )
    except Exception as e:
        logger.error("CTEM pass '%s' error: %s", name, format_exception_message(e), exc_info=True)
    return time.monotonic() - started


async def run_passes(passes: tuple[tuple[str, Pass], ...] = PASSES) -> dict[str, float]:
    """Every pass once, one after the other; the duration of each. A long cycle is logged with its slowest pass."""
    took = {name: await run_pass(name, call) for name, call in passes}
    total = sum(took.values())
    if took and total > CYCLE_WARNING_SECONDS:
        slowest = max(took, key=lambda name: took[name])
        logger.warning("CTEM background passes took %.1fs, slowest pass '%s' (%.1fs)", total, slowest, took[slowest])
    return took


async def run_cycle() -> bool:
    """One cycle under the lock; True when this process ran it. Without Redis the cycle runs (each pass is idempotent)."""
    token = uuid.uuid4().hex
    redis: Any = None
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        if not await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
            return False
    except Exception:
        redis = None
    try:
        await run_passes()
    finally:
        if redis is not None:
            try:
                await redis.eval(_UNLOCK_SCRIPT, 1, LOCK_KEY, token)
            except Exception:
                logger.debug("CTEM background passes: lock release failed; it expires by itself", exc_info=True)
    return True


async def run_loop() -> None:
    """The background task ``main.py`` starts: a cycle every :data:`LOOP_SECONDS`, never on the scheduler tick."""
    while True:
        try:
            await run_cycle()
        except Exception as e:
            logger.error("CTEM background passes error: %s", format_exception_message(e), exc_info=True)
        await asyncio.sleep(LOOP_SECONDS)
