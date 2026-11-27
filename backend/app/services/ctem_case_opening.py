# SPDX-License-Identifier: Apache-2.0

"""The administrator switch that pauses and resumes CTEM case opening (#1951).

A confirmed, in-scope signal opens (or joins) its case on three paths, all
through ``ctem_case_loop.open_from_signal``: the scope gate's confirmation
(``ctem_scope_gate_store.evaluate_after_record``), a source read
(``ctem_cases_store._case_of_signal``) and the scheduled sweep
(``ctem_case_loop.sweep``). While case opening is paused for a vertical, none
of them opens a case: the signal stays ``confirmed`` with ``case_id`` null and
waits. The rest of the sweep (SLA breaches, announce, stall) keeps running; a
manual split still creates a case.

The pause is enforced at the single choke point too (#2536):
``open_from_signal`` and ``join_case`` read the switch fresh from the database
before they link a signal, ``open_waiting`` re-reads it before every signal and
stops at once, and turning it off cancels the drains this process runs. A drain
or a sweep already in flight when an administrator turns case opening off
opens nothing after that commit.

The switch is one persisted platform setting,
``platform_settings.ctem_case_opening`` (``{vertical: false}`` pauses it; NULL
or a missing vertical is on, the behaviour before the switch). Turning it on
starts :func:`drain`: sweep passes back to back until no confirmed signal
waits, each pass committed and its live frames published, so the Cases list
fills in seconds instead of at the next sweep slot. Two drains of one
vertical never run a pass at the same time (a PostgreSQL advisory lock per
pass), so a signal never opens two cases.

dev-docs/ctem-cases.md section 4.6 is the contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.ctem_signal import CtemSignal
from app.models.user import User

logger = logging.getLogger(__name__)

#: The platform event raised when an administrator turns case opening on or off.
OPENING_CHANGED = "ctem_case.opening_changed"
#: Most sweep passes one drain runs (a pass opens up to ``ctem_case_loop.SWEEP_BATCH`` signals).
DRAIN_MAX_PASSES = 100
#: The advisory lock key prefix that keeps two drain passes of one vertical apart.
_DRAIN_LOCK = "ctem-case-drain:"
#: A drain pass never blocks on the vertical lock (#2555): a busy lock (a recompute's write phase,
#: another drain's pass) is tried again after a backoff that doubles from the first value to the
#: cap, for at most the budget; the drain then stops with a log line, and the sweep takes over.
DRAIN_BUSY_FIRST_WAIT_SECONDS = 0.5
DRAIN_BUSY_MAX_WAIT_SECONDS = 10.0
DRAIN_BUSY_BUDGET_SECONDS = 300.0
#: The pause between two drain passes (#2555): the passes of a large backlog never hold the
#: database back to back, so the requests and the webhooks of the platform get their connections.
DRAIN_PASS_PAUSE_SECONDS = 0.5

#: The drains started by a PUT, held so the event loop never drops one mid-run.
_DRAINS: set[asyncio.Task[Any]] = set()

#: Most new cases one vertical opens per minute when ``CTEM_CASE_OPEN_PER_MINUTE`` is not set (#2570).
OPEN_PER_MINUTE_DEFAULT = 10
#: The Redis key prefix of the per-minute opening budget of a vertical (#2570).
_BUDGET_KEY = "ctem:cases:open-budget:"
#: The Redis slot of the paced opening pass the autonomous tick runs once a minute (#2570).
_PACE_KEY = "ctem:cases:open-pace"
#: The process-local budget used when Redis is unreachable: never fail open during a storm.
_LOCAL_BUDGET: dict[str, int] = {}


def open_per_minute() -> int:
    """``CTEM_CASE_OPEN_PER_MINUTE`` (0 or less: no limit), else :data:`OPEN_PER_MINUTE_DEFAULT`."""
    import os

    try:
        return int(os.environ.get("CTEM_CASE_OPEN_PER_MINUTE") or OPEN_PER_MINUTE_DEFAULT)
    except ValueError:
        return OPEN_PER_MINUTE_DEFAULT


def _budget_key(vertical: str, now: float | None = None) -> str:
    minute = int((time.time() if now is None else now) // 60)
    return f"{_BUDGET_KEY}{vertical}:{minute}"


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def opened_this_minute(vertical: str) -> int:
    """How many new cases *vertical* opened in the current minute (the budget read, nothing taken)."""
    key = _budget_key(vertical)
    try:
        client = await _redis()
        return int(await client.get(key) or 0)
    except Exception:
        return _LOCAL_BUDGET.get(key, 0)


async def budget_left(vertical: str) -> int | None:
    """The new cases *vertical* may still open this minute; None when opening is not rate-limited."""
    limit = open_per_minute()
    if limit <= 0:
        return None
    return max(0, limit - await opened_this_minute(vertical))


async def take_opening_token(vertical: str) -> bool:
    """Take one new case of the per-minute budget of *vertical* (#2570); False when it is spent.

    Every automatic opening path (scope gate confirmation, source read, sweep, drain, paced pass)
    goes through ``ctem_case_loop.open_from_signal``, which takes a token before it creates a
    case: a storm of confirmed signals opens at most the budget per minute, each case keeping its
    own supervising run, and the rest stays ``confirmed`` and opens on the next passes. A signal
    that joins an open case takes no token. Redis unreachable: a process-local budget, never none.
    """
    limit = open_per_minute()
    if limit <= 0:
        return True
    key = _budget_key(vertical)
    try:
        client = await _redis()
        used = int(await client.incr(key))
        if used == 1:
            await client.expire(key, 120)
    except Exception:
        for stale in [k for k in _LOCAL_BUDGET if k.startswith(f"{_BUDGET_KEY}{vertical}:") and k != key]:
            _LOCAL_BUDGET.pop(stale, None)
        used = _LOCAL_BUDGET[key] = _LOCAL_BUDGET.get(key, 0) + 1
    return used <= limit


async def paced_pass_due() -> bool:
    """Take the slot of the paced opening pass for this minute; True when this tick runs it (or no Redis)."""
    try:
        client = await _redis()
        return bool(await client.set(_PACE_KEY, "1", nx=True, ex=60))
    except Exception:
        return True


async def paced_pass(vertical: str = "cyber", *, session_factory: Any = None) -> dict[str, int] | None:
    """One opening pass within the per-minute budget, run by the autonomous tick (#2570).

    What lets a platform restarted in a storm recover on its own: the drain a PUT started is gone
    with the process, the hourly sweep is too slow, and nobody has to reach the UI. Each minute
    this pass opens at most the budget of the waiting confirmed signals, in its own session.
    None when the vertical lock is busy (the next minute tries again).
    """
    if session_factory is None:
        from app.core.database import async_session as session_factory
    left = await budget_left(vertical)
    if left == 0:
        return {"opened": 0, "joined": 0, "skipped": 0, "throttled": 1}
    async with session_factory() as db:
        if not await waiting_count(db):
            return {"opened": 0, "joined": 0, "skipped": 0}
        return await drain_pass(db, vertical)


async def paused_verticals(db: AsyncSession) -> set[str]:
    """The verticals whose case opening an administrator paused (empty: on everywhere)."""
    return await read_paused_verticals(db)


async def read_paused_verticals(db: AsyncSession) -> set[str]:
    """The stored read behind :func:`paused_verticals` (the unit suite stubs that one; tests/conftest.py)."""
    from app.models.platform_settings import PlatformSettings

    # A column read, never the session's identity-map copy of the row: a drain or a sweep that holds
    # its transaction for seconds sees the switch an administrator committed meanwhile (#2536).
    stored = await db.scalar(
        select(PlatformSettings.ctem_case_opening)
        .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
        .limit(1)
    )
    return {str(vertical) for vertical, on in (stored or {}).items() if on is False}


async def is_on(db: AsyncSession, vertical: str) -> bool:
    """True when case opening runs for *vertical* (the default)."""
    return vertical not in await paused_verticals(db)


async def may_open_waiting(db: AsyncSession, vertical: str) -> bool:
    """Whether the sweep opens the cases of the waiting signals of *vertical* this pass."""
    return await is_on(db, vertical)


async def waiting_count(db: AsyncSession) -> int:
    """The confirmed signals that wait for their case (``case_id`` null)."""
    value = await db.scalar(
        select(func.count())
        .select_from(CtemSignal)
        .where(CtemSignal.state == "confirmed", CtemSignal.case_id.is_(None))
    )
    return int(value or 0)


async def state(db: AsyncSession, vertical: str) -> dict[str, Any]:
    """``{vertical, on, waiting}``: what ``GET /ctem-cases/opening`` answers."""
    return {"vertical": vertical, "on": await is_on(db, vertical), "waiting": await waiting_count(db)}


async def set_opening(db: AsyncSession, *, vertical: str, on: bool, actor: User) -> bool:
    """Turn case opening of *vertical* on or off; True when the value changed. The caller commits.

    Every call that changes the value writes one audit row (who, when, on or off).
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services.audit import log_audit

    settings_row = await get_platform_settings(db)
    stored = dict(settings_row.ctem_case_opening or {})
    was_on = stored.get(vertical) is not False
    if was_on == on:
        return False
    if on:
        stored.pop(vertical, None)
    else:
        stored[vertical] = False
        # #2536: a drain started by an earlier "on" stops now; its pass in flight rolls back.
        stop_drains(vertical)
    settings_row.ctem_case_opening = stored or None
    flag_modified(settings_row, "ctem_case_opening")
    await log_audit(
        db,
        user=actor,
        action="update",
        entity_type="ctem_case_opening",
        entity_name=f"CTEM case opening ({vertical})",
        summary=f"Turned CTEM case opening {'on' if on else 'off'} for {vertical}",
        details={"vertical": vertical, "on": on, "changed_at": datetime.now(UTC).isoformat()},
    )
    return True


async def hold_vertical_lock(db: AsyncSession, vertical: str) -> None:
    """Take the transaction-scoped advisory lock that keeps the case writers of *vertical* apart.

    A drain pass and the write phase of a source recompute (``ctem_cases_source.recompute_for_user``)
    both write the vertical's signals and cases: holding one lock, they never interleave their row
    locks on ``ctem_signals`` and never deadlock (#2084). Released at the commit or the rollback.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _DRAIN_LOCK + vertical})


async def try_vertical_lock(db: AsyncSession, vertical: str) -> bool:
    """Take the lock of :func:`hold_vertical_lock` without waiting; False when another writer holds it (#2555).

    The blocking form made a drain pass wait on a long recompute until the statement timeout
    killed the drain after 0 passes; the try form answers at once and the drain backs off.
    """
    taken = await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": _DRAIN_LOCK + vertical})
    return bool(taken)


def event_data(vertical: str, on: bool, actor: User, waiting: int) -> dict[str, Any]:
    """The payload of ``ctem_case.opening_changed``."""
    return {"vertical": vertical, "on": on, "changed_by": str(actor.id), "waiting": waiting}


async def drain_pass(db: AsyncSession, vertical: str) -> dict[str, int] | None:
    """One drain pass: the waiting signals of one sweep batch opened, committed, frames published.

    Holds a transaction-scoped advisory lock: a second drain of *vertical* backs off until this
    pass commits and then finds those signals linked, so a fix never opens two cases. None when
    the lock is busy (#2555): nothing was read or written, the caller tries again later.
    """
    from app.services import ctem_case_loop as loop

    counts = {"opened": 0, "joined": 0, "skipped": 0}
    if not await try_vertical_lock(db, vertical):
        await db.rollback()
        return None
    if not await is_on(db, vertical):
        await db.rollback()
        return counts
    events = loop.PendingEvents()
    throttled = await loop.open_waiting(db, vertical=vertical, events=events, now=datetime.now(UTC), counts=counts)
    if throttled:
        counts["throttled"] = 1
    await db.commit()
    await loop.publish_pending(events)
    return counts


async def drain(vertical: str, *, session_factory: Any = None, max_passes: int = DRAIN_MAX_PASSES) -> dict[str, int]:
    """Open the cases of every waiting confirmed signal now, pass after pass; the totals.

    Stops when a pass opens and joins nothing (none waits, or what waits cannot be opened), when
    the per-minute opening budget is spent (#2570: the paced pass takes over), when
    case opening is paused again, after *max_passes*, or when the vertical lock stays busy past
    :data:`DRAIN_BUSY_BUDGET_SECONDS` (#2555). Passes are spaced by :data:`DRAIN_PASS_PAUSE_SECONDS`.
    Logged at the end. Never raises.
    """
    if session_factory is None:
        from app.core.database import async_session as session_factory
    totals = {"passes": 0, "opened": 0, "joined": 0, "skipped": 0}
    started = time.monotonic()
    try:
        while totals["passes"] < max_passes:
            if totals["passes"]:
                await asyncio.sleep(DRAIN_PASS_PAUSE_SECONDS)
            counts = await _locked_pass(vertical, session_factory)
            if counts is None:
                logger.warning(
                    "CTEM case opening drain of %s stopped: vertical lock busy for %.0f s, the sweep opens the rest",
                    vertical,
                    DRAIN_BUSY_BUDGET_SECONDS,
                )
                break
            totals["passes"] += 1
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            if counts["opened"] + counts["joined"] == 0:
                break
            if counts.get("throttled"):
                # #2570: the per-minute budget is spent; the paced pass of the autonomous tick
                # opens the rest, one budget a minute, and survives a restart.
                break
    except Exception:
        logger.exception("CTEM case opening drain of %s stopped after %s passes", vertical, totals["passes"])
    logger.info("CTEM case opening drain of %s: %s in %.1f s", vertical, totals, time.monotonic() - started)
    return totals


async def _locked_pass(vertical: str, session_factory: Any) -> dict[str, int] | None:
    """One :func:`drain_pass`, tried again with a doubling backoff while the lock is busy; None past the budget."""
    wait = DRAIN_BUSY_FIRST_WAIT_SECONDS
    deadline = time.monotonic() + DRAIN_BUSY_BUDGET_SECONDS
    while True:
        async with session_factory() as db:
            counts = await drain_pass(db, vertical)
        if counts is not None:
            return counts
        if time.monotonic() + wait > deadline:
            return None
        logger.info("CTEM case opening drain of %s: vertical lock busy, next try in %.1f s", vertical, wait)
        await asyncio.sleep(wait)
        wait = min(wait * 2, DRAIN_BUSY_MAX_WAIT_SECONDS)


def _drain_name(vertical: str) -> str:
    return f"ctem-case-opening-drain:{vertical}"


def stop_drains(vertical: str) -> int:
    """Cancel the drains of *vertical* this process runs; answers how many (#2536).

    A cancelled pass rolls back with its session, so the cases it was opening never land. A drain
    of another worker stops at its next signal: ``open_waiting`` re-reads the switch before each.
    """
    stopped = 0
    for task in list(_DRAINS):
        if task.get_name() == _drain_name(vertical) and not task.done():
            task.cancel()
            stopped += 1
    if stopped:
        logger.info("CTEM case opening paused for %s: %s drain(s) stopped", vertical, stopped)
    return stopped


def start_drain(vertical: str) -> asyncio.Task[Any]:
    """Run :func:`drain` in the background (the PUT answers at once)."""
    task = asyncio.create_task(drain(vertical), name=_drain_name(vertical))
    _DRAINS.add(task)
    task.add_done_callback(_DRAINS.discard)
    return task
