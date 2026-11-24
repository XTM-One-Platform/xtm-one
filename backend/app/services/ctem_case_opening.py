"""The administrator switch that pauses and resumes CTEM case opening (#1951).

A confirmed, in-scope signal opens (or joins) its case on three paths, all
through ``ctem_case_loop.open_from_signal``: the scope gate's confirmation
(``ctem_scope_gate_store.evaluate_after_record``), a source read
(``ctem_cases_store._case_of_signal``) and the scheduled sweep
(``ctem_case_loop.sweep``). While case opening is paused for a vertical, none
of them opens a case: the signal stays ``confirmed`` with ``case_id`` null and
waits. The rest of the sweep (SLA breaches, announce, stall) keeps running; a
manual split still creates a case.

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

#: The drains started by a PUT, held so the event loop never drops one mid-run.
_DRAINS: set[asyncio.Task[Any]] = set()


async def paused_verticals(db: AsyncSession) -> set[str]:
    """The verticals whose case opening an administrator paused (empty: on everywhere)."""
    return await read_paused_verticals(db)


async def read_paused_verticals(db: AsyncSession) -> set[str]:
    """The stored read behind :func:`paused_verticals` (the unit suite stubs that one; tests/conftest.py)."""
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    settings_row = await peek_platform_settings(db)
    stored = (settings_row.ctem_case_opening if settings_row is not None else None) or {}
    return {str(vertical) for vertical, on in stored.items() if on is False}


async def is_on(db: AsyncSession, vertical: str) -> bool:
    """True when case opening runs for *vertical* (the default)."""
    return vertical not in await paused_verticals(db)


async def may_open_waiting(db: AsyncSession, vertical: str) -> bool:
    """Whether the sweep opens the cases of the waiting signals of *vertical* this pass."""
    return await is_on(db, vertical)



async def waiting_count(db: AsyncSession) -> int:
    """The confirmed signals that wait for their case (``case_id`` null)."""
    value = await db.scalar(
        select(func.count()).select_from(CtemSignal).where(CtemSignal.state == "confirmed", CtemSignal.case_id.is_(None))
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
    settings_row.ctem_case_opening = stored or None
    flag_modified(settings_row, "ctem_case_opening")
    await log_audit(
        db,
        user=actor,
        action="update",
        entity_type="ctem_case_opening",
        entity_id=vertical,
        entity_name=f"CTEM case opening ({vertical})",
        summary=f"Turned CTEM case opening {'on' if on else 'off'} for {vertical}",
        details={"vertical": vertical, "on": on, "changed_at": datetime.now(UTC).isoformat()},
    )
    return True


def event_data(vertical: str, on: bool, actor: User, waiting: int) -> dict[str, Any]:
    """The payload of ``ctem_case.opening_changed``."""
    return {"vertical": vertical, "on": on, "changed_by": str(actor.id), "waiting": waiting}


async def drain_pass(db: AsyncSession, vertical: str) -> dict[str, int]:
    """One drain pass: the waiting signals of one sweep batch opened, committed, frames published.

    Holds a transaction-scoped advisory lock: a second drain of *vertical* waits for this pass's
    commit and then finds those signals linked, so a fix never opens two cases.
    """
    from app.services import ctem_case_loop as loop

    counts = {"opened": 0, "joined": 0, "skipped": 0}
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _DRAIN_LOCK + vertical})
    if not await is_on(db, vertical):
        await db.rollback()
        return counts
    events = loop.PendingEvents()
    await loop.open_waiting(db, vertical=vertical, events=events, now=datetime.now(UTC), counts=counts)
    await db.commit()
    await loop.publish_pending(events)
    return counts


async def drain(vertical: str, *, session_factory: Any = None, max_passes: int = DRAIN_MAX_PASSES) -> dict[str, int]:
    """Open the cases of every waiting confirmed signal now, pass after pass; the totals.

    Stops when a pass opens and joins nothing (none waits, or what waits cannot be opened), when
    case opening is paused again, or after *max_passes*. Logged at the end. Never raises.
    """
    if session_factory is None:
        from app.core.database import async_session as session_factory
    totals = {"passes": 0, "opened": 0, "joined": 0, "skipped": 0}
    started = time.monotonic()
    try:
        for _ in range(max_passes):
            async with session_factory() as db:
                counts = await drain_pass(db, vertical)
            totals["passes"] += 1
            for key, value in counts.items():
                totals[key] += value
            if counts["opened"] + counts["joined"] == 0:
                break
    except Exception:
        logger.exception("CTEM case opening drain of %s stopped after %s passes", vertical, totals["passes"])
    logger.info(
        "CTEM case opening drain of %s: %s in %.1f s", vertical, totals, time.monotonic() - started
    )
    return totals


def start_drain(vertical: str) -> asyncio.Task[Any]:
    """Run :func:`drain` in the background (the PUT answers at once)."""
    task = asyncio.create_task(drain(vertical), name=f"ctem-case-opening-drain:{vertical}")
    _DRAINS.add(task)
    task.add_done_callback(_DRAINS.discard)
    return task
