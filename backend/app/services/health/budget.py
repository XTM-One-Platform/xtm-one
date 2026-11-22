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

"""Platform Health's dedicated budget: its own line of the quota (#8254).

PRD ``dev-docs/platform-concierge.md`` 6.12 and decision B20. Both editions:
Platform Health work first passes the one licence gate every background work passes
(``agentic_gate.agentic_verdict``: the AI switch and a licensed edition), then
its own caps, set in Settings > Agents and automations > Platform Health: per cycle and per month, in tokens
and in dollars (invariant 17: a quota, not a feature switch). It is an extra
cap, never a bypass: the fleet's runs are company-managed assignment runs and
count in the platform-wide action tiers as any other. The one exception is the
Module Analyst (#101 S14b, dev-docs ``platform-health.md`` 12.5): its runs leave
the agentic quota for a ceiling of their own (``module_analyst_budget.py``).

* A **cycle** (``concierge_cycles``) is one unit of work, opened before a
  specialist starts (:func:`open_cycle`) and closed when it ends
  (:func:`close_cycle`). Its spend is booked on the month it was opened in.
* Work **reserves** what it may spend before it starts (:func:`reserve`),
  refused when a cap would be passed, then **charges** what it really spent
  (:func:`charge`), which releases the reservation. A charge is booked even
  past a cap - spend is a fact - and then no new work starts.
* Every write locks the month row first, then the cycle row (``FOR UPDATE``),
  so concurrent reservations cannot pass a cap together.
* Work that outlives its cycle (an evaluation) **holds** its reservation
  (``reserve(hold=...)``): closing the cycle keeps it until the work's spend
  is charged. A charge naming its work (``ref``) is booked once.
* When the budget is reached nothing new starts, and the reason is a
  :class:`BudgetRefusedError` whose message the digest states.
* The AI gate is read where a cycle starts (:func:`open_cycle`,
  :func:`budget_refusal`), never by :func:`reserve` or :func:`remaining`, which
  a run in flight calls: turning AI off stops new work, not a run under way.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthBudgetMonth, HealthBudgetWork, HealthCycle
from app.services.health.availability import health_active
from app.services.health.identity import HEALTH_ROLES
from app.services.health.settings import HealthBudgetCaps, load_health_settings

logger = logging.getLogger(__name__)

BudgetRefusalCode = Literal[
    "concierge_disabled",
    "ai_disabled",
    "license_required",
    "transient",
    "cycle_closed",
    "cycle_budget_reached",
    "month_budget_reached",
]

#: What the digest and the callers state for each refusal; ``{name}`` is Platform Health's name (``naming``).
REFUSAL_MESSAGES: dict[str, str] = {
    "concierge_disabled": "{name} is switched off.",
    "ai_disabled": "Agentic AI is switched off on this platform.",
    "license_required": "Agentic AI is not licensed on this platform.",
    "transient": "Whether agentic AI may run could not be read; nothing was started.",
    "cycle_closed": "This cycle of {name} is closed.",
    "cycle_budget_reached": "{name}'s budget for this cycle is reached.",
    "month_budget_reached": "{name}'s budget for this month is reached.",
}


def refusal_message(code: str, name: str) -> str | None:
    """The refusal *code* told with Platform Health called *name*, ``None`` for an unknown code.

    ``str.replace``, never ``format``: a brace in a name stays a brace.
    """
    template = REFUSAL_MESSAGES.get(code)
    return template.replace("{name}", name) if template is not None else None


CycleTrigger = Literal["schedule", "signal", "on_demand"]
CycleEnd = Literal["completed", "budget_reached", "failed", "cancelled"]
_TRIGGERS = frozenset({"schedule", "signal", "on_demand"})
_ENDS = frozenset({"completed", "budget_reached", "failed", "cancelled"})
_CENT_MICROS = Decimal("0.000001")


class BudgetRefusedError(Exception):
    """New Platform Health work may not start. ``message`` is what the digest says."""

    def __init__(self, code: BudgetRefusalCode) -> None:
        self.code = code
        # The digest and the tools read it: the platform's own name, no administrator text.
        self.message = REFUSAL_MESSAGES[code].replace("{name}", "Platform Health")
        super().__init__(self.message)


#: A piece of work the ledger knows by its id: ``(kind, id)``.
WorkRef = tuple[str, uuid.UUID]


@dataclass(frozen=True, slots=True)
class Reservation:
    cycle_id: uuid.UUID
    tokens: int
    cost_usd: Decimal
    #: The work holding it past its cycle's close, ``None`` for work within the cycle.
    hold: WorkRef | None = None


@dataclass(frozen=True, slots=True)
class BudgetRemaining:
    """What is left; ``None`` on a dimension with no cap. ``refusal``: why nothing new may start, or ``None``."""

    cycle_tokens: int | None
    cycle_cost_usd: Decimal | None
    month_tokens: int | None
    month_cost_usd: Decimal | None
    refusal: BudgetRefusalCode | None


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")


def _cost(value: Decimal | float | int | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        amount = Decimal("NaN")
    if not amount.is_finite() or amount < 0:
        raise ValueError("a cost is a non-negative amount")
    return amount.quantize(_CENT_MICROS, rounding=ROUND_HALF_UP)


def _tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("a token count is a non-negative integer")
    return value


def _left(cap: int | Decimal | None, used: int | Decimal) -> int | Decimal | None:
    return None if cap is None else max(cap - used, type(cap)(0))


def _passes(cap: int | Decimal | None, used: int | Decimal, extra: int | Decimal) -> bool:
    """Whether *cap* is already reached or *used* + *extra* passes it (reaching it exactly does not)."""
    return cap is not None and (used >= cap or used + extra > cap)


def _reached(cap: int | Decimal | None, used: int | Decimal) -> bool:
    return cap is not None and used >= cap


async def _gate(db: AsyncSession) -> BudgetRefusalCode | None:
    """The switches and the licence, before any cap."""
    if not await health_active(db):
        return "concierge_disabled"
    from app.services.agentic_gate import agentic_verdict

    verdict = await agentic_verdict(db)
    if verdict.refusal is None:
        return None
    if verdict.transient:
        return "transient"
    return verdict.refusal if verdict.refusal in REFUSAL_MESSAGES else "ai_disabled"


async def _month_row(db: AsyncSession, month: str) -> HealthBudgetMonth | None:
    """The month's row as it is, ``None`` before its first reservation. Reads only."""
    query = select(HealthBudgetMonth).where(HealthBudgetMonth.month == month)
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


async def _locked_month(db: AsyncSession, month: str) -> HealthBudgetMonth:
    """The month's row, created when missing, locked for the transaction: what every write locks first."""
    await db.execute(pg_insert(HealthBudgetMonth).values(month=month).on_conflict_do_nothing())
    query = select(HealthBudgetMonth).where(HealthBudgetMonth.month == month).with_for_update()
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one()


async def _cycle_row(db: AsyncSession, cycle_id: uuid.UUID, *, lock: bool) -> HealthCycle | None:
    query = select(HealthCycle).where(HealthCycle.id == cycle_id)
    if lock:
        query = query.with_for_update()
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


def _cap_refusal(
    caps: HealthBudgetCaps,
    month: HealthBudgetMonth | None,
    cycle: HealthCycle | None,
    *,
    tokens: int = 0,
    cost: Decimal = Decimal(0),
) -> BudgetRefusalCode | None:
    """Which cap is reached, or which *tokens* / *cost* more would pass.

    A month with no row yet has used nothing: a cap of 0 refuses all the same.
    """
    check = _passes if tokens or cost else (lambda cap, used, _extra: _reached(cap, used))
    m_tokens = (month.spent_tokens + month.reserved_tokens) if month is not None else 0
    m_cost = (month.spent_cost_usd + month.reserved_cost_usd) if month is not None else Decimal(0)
    if check(caps.month_tokens, m_tokens, tokens) or check(caps.month_cost_usd, m_cost, cost):
        return "month_budget_reached"
    if cycle is not None and (
        check(caps.cycle_tokens, cycle.spent_tokens + cycle.reserved_tokens, tokens)
        or check(caps.cycle_cost_usd, cycle.spent_cost_usd + cycle.reserved_cost_usd, cost)
    ):
        return "cycle_budget_reached"
    return None


async def budget_refusal(db: AsyncSession, cycle_id: uuid.UUID | None = None) -> BudgetRefusalCode | None:
    """Why a new Platform Health cycle may not start now, or ``None`` when it may.

    The entry gate: the licence first, then the caps. A run in flight asks
    :func:`remaining` instead, which never reads the AI gate.
    """
    refusal = await _gate(db)
    if refusal is not None:
        return refusal
    return await _cap_state(db, cycle_id)


async def _cap_state(db: AsyncSession, cycle_id: uuid.UUID | None) -> BudgetRefusalCode | None:
    """Platform Health's own switch and caps, without the AI gate."""
    if not await health_active(db):
        return "concierge_disabled"
    caps = (await load_health_settings(db)).budget
    cycle = await _cycle_row(db, cycle_id, lock=False) if cycle_id is not None else None
    if cycle_id is not None and (cycle is None or cycle.status != "open"):
        return "cycle_closed"
    month = await _month_row(db, cycle.month if cycle is not None else current_month())
    return _cap_refusal(caps, month, cycle)


async def open_cycle(
    db: AsyncSession,
    *,
    specialist: str,
    trigger: CycleTrigger,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Open a cycle for *specialist*; raises :class:`BudgetRefusedError` when work may not start. Flushed."""
    if specialist not in HEALTH_ROLES:
        raise ValueError(f"not a Platform Health role: {specialist!r}")
    if trigger not in _TRIGGERS:
        raise ValueError(f"not a cycle trigger: {trigger!r}")
    # The module checkers are the one exception to the fleet caps (#101 S14b-2 and S15, dev-docs
    # 12.5, 13.1): the switches and the licence still gate their cycles, their own ceiling bounds
    # their spend.
    from app.services.health.identity import MODULE_CHECKER_ROLES

    refusal = await _gate(db) if specialist in MODULE_CHECKER_ROLES else await budget_refusal(db)
    if refusal is not None:
        raise BudgetRefusedError(refusal)
    cycle = HealthCycle(
        specialist=specialist,
        trigger=trigger,
        target_type=target_type,
        target_id=target_id,
        month=current_month(),
    )
    db.add(cycle)
    await db.flush()
    return cycle.id


async def reserve(
    db: AsyncSession, cycle_id: uuid.UUID, *, tokens: int, cost_usd: Decimal, hold: WorkRef | None = None
) -> Reservation:
    """Set aside *tokens* / *cost_usd* for work about to start in *cycle_id*. Flushed.

    Raises :class:`BudgetRefusedError` when Platform Health is switched off,
    the cycle is closed, or a cap is reached or would be passed. Called from a
    run in flight, it never reads the AI gate: the cycle passed it when it
    opened (``tests/test_ai_toggle.py``). *hold*: the work keeps the
    reservation past the cycle's close, until its charge or release.
    """
    tokens, cost = _tokens(tokens), _cost(cost_usd)
    if not await health_active(db):
        raise BudgetRefusedError("concierge_disabled")
    caps = (await load_health_settings(db)).budget
    peek = await _cycle_row(db, cycle_id, lock=False)
    if peek is None:
        raise BudgetRefusedError("cycle_closed")
    month = await _locked_month(db, peek.month)
    cycle = await _cycle_row(db, cycle_id, lock=True)
    if cycle is None or cycle.status != "open":
        raise BudgetRefusedError("cycle_closed")
    refusal = _cap_refusal(caps, month, cycle, tokens=tokens, cost=cost)
    if refusal is not None:
        raise BudgetRefusedError(refusal)
    month.reserved_tokens += tokens
    month.reserved_cost_usd += cost
    cycle.reserved_tokens += tokens
    cycle.reserved_cost_usd += cost
    if hold is not None:
        db.add(
            HealthBudgetWork(kind=hold[0], ref_id=hold[1], cycle_id=cycle_id, held_tokens=tokens, held_cost_usd=cost)
        )
    await db.flush()
    return Reservation(cycle_id=cycle_id, tokens=tokens, cost_usd=cost, hold=hold)


def _unreserve(month: HealthBudgetMonth, cycle: HealthCycle, tokens: int, cost: Decimal) -> None:
    """Give back what *cycle* still holds of a reservation, from the cycle and the month alike.

    Never more than the cycle holds: a cycle closed meanwhile already gave its
    reservations back, and the month's reservations are other cycles' too.
    """
    tokens = min(tokens, cycle.reserved_tokens)
    cost = min(cost, cycle.reserved_cost_usd)
    for row in (month, cycle):
        row.reserved_tokens = max(row.reserved_tokens - tokens, 0)
        row.reserved_cost_usd = max(row.reserved_cost_usd - cost, Decimal(0))


async def _locked_pair(db: AsyncSession, cycle_id: uuid.UUID) -> tuple[HealthBudgetMonth, HealthCycle]:
    peek = await _cycle_row(db, cycle_id, lock=False)
    if peek is None:
        raise ValueError(f"no Platform Health cycle {cycle_id}")
    month = await _locked_month(db, peek.month)
    cycle = await _cycle_row(db, cycle_id, lock=True)
    if cycle is None:
        raise ValueError(f"no Platform Health cycle {cycle_id}")
    return month, cycle


async def _work_row(db: AsyncSession, ref: WorkRef) -> HealthBudgetWork | None:
    query = select(HealthBudgetWork).where(HealthBudgetWork.kind == ref[0], HealthBudgetWork.ref_id == ref[1])
    return (await db.execute(query.with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()


async def charge(
    db: AsyncSession,
    cycle_id: uuid.UUID,
    *,
    tokens: int,
    cost_usd: Decimal,
    reservation: Reservation | None = None,
    ref: WorkRef | None = None,
) -> bool:
    """Book what work in *cycle_id* really spent, releasing *reservation*. Flushed.

    Booked whatever the caps and the cycle's state: spend is a fact. The work
    *ref* (a held reservation's own by default) is booked once: ``False`` when
    it already was, and nothing changes.
    """
    tokens, cost = _tokens(tokens), _cost(cost_usd)
    if reservation is not None and reservation.cycle_id != cycle_id:
        raise ValueError("the reservation is another cycle's")
    ref = ref or (reservation.hold if reservation is not None else None)
    month, cycle = await _locked_pair(db, cycle_id)
    work = await _work_row(db, ref) if ref is not None else None
    if work is not None and work.charged_at is not None:
        return False
    if reservation is not None:
        _unreserve(month, cycle, reservation.tokens, reservation.cost_usd)
    for row in (month, cycle):
        row.spent_tokens += tokens
        row.spent_cost_usd += cost
    if ref is not None:
        if work is None:
            work = HealthBudgetWork(kind=ref[0], ref_id=ref[1], cycle_id=cycle_id)
            db.add(work)
        work.held_tokens, work.held_cost_usd, work.charged_at = 0, Decimal(0), datetime.now(UTC)
    await db.flush()
    return True


async def release(db: AsyncSession, reservation: Reservation) -> None:
    """Give back a reservation whose work did not run. Flushed."""
    month, cycle = await _locked_pair(db, reservation.cycle_id)
    _unreserve(month, cycle, reservation.tokens, reservation.cost_usd)
    if reservation.hold is not None and (work := await _work_row(db, reservation.hold)) is not None:
        await db.delete(work)
    await db.flush()


async def charged_refs(db: AsyncSession, kind: str, ref_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of *ref_ids* (work of *kind*) already had their spend charged."""
    if not ref_ids:
        return set()
    query = select(HealthBudgetWork.ref_id).where(
        HealthBudgetWork.kind == kind,
        HealthBudgetWork.ref_id.in_(ref_ids),
        HealthBudgetWork.charged_at.is_not(None),
    )
    return set((await db.execute(query)).scalars())


async def held_reservations(db: AsyncSession, kind: str, *, limit: int = 100) -> list[Reservation]:
    """The reservations work of *kind* still holds (not charged yet), oldest first."""
    rows = (
        await db.execute(
            select(HealthBudgetWork)
            .where(HealthBudgetWork.kind == kind, HealthBudgetWork.charged_at.is_(None))
            .order_by(HealthBudgetWork.created_at)
            .limit(limit)
        )
    ).scalars()
    return [
        Reservation(cycle_id=w.cycle_id, tokens=w.held_tokens, cost_usd=w.held_cost_usd, hold=(w.kind, w.ref_id))
        for w in rows
    ]


async def remaining(db: AsyncSession, cycle_id: uuid.UUID | None = None) -> BudgetRemaining:
    """What is left of the month (and of *cycle_id*), and why no more work fits, if so.

    Read by a run in flight: Platform Health's own switch and caps, never the AI gate.
    """
    caps = (await load_health_settings(db)).budget
    cycle = await _cycle_row(db, cycle_id, lock=False) if cycle_id is not None else None
    month = await _month_row(db, cycle.month if cycle is not None else current_month())
    m_tokens = (month.spent_tokens + month.reserved_tokens) if month else 0
    m_cost = (month.spent_cost_usd + month.reserved_cost_usd) if month else Decimal(0)
    c_tokens = (cycle.spent_tokens + cycle.reserved_tokens) if cycle else 0
    c_cost = (cycle.spent_cost_usd + cycle.reserved_cost_usd) if cycle else Decimal(0)
    return BudgetRemaining(
        cycle_tokens=_left(caps.cycle_tokens, c_tokens),
        cycle_cost_usd=_left(caps.cycle_cost_usd, c_cost),
        month_tokens=_left(caps.month_tokens, m_tokens),
        month_cost_usd=_left(caps.month_cost_usd, m_cost),
        refusal=await _cap_state(db, cycle_id),
    )


async def close_cycle(db: AsyncSession, cycle_id: uuid.UUID, *, status: CycleEnd, reason: str | None = None) -> None:
    """End a cycle; what it still held in reservation goes back to the month,
    but what work still running holds (``reserve(hold=...)``). Flushed. Idempotent."""
    if status not in _ENDS:
        raise ValueError(f"not a cycle end: {status!r}")
    month, cycle = await _locked_pair(db, cycle_id)
    if cycle.status != "open":
        return
    held_tokens, held_cost = (
        await db.execute(
            select(
                func.coalesce(func.sum(HealthBudgetWork.held_tokens), 0),
                func.coalesce(func.sum(HealthBudgetWork.held_cost_usd), 0),
            ).where(HealthBudgetWork.cycle_id == cycle_id, HealthBudgetWork.charged_at.is_(None))
        )
    ).one()
    _unreserve(
        month,
        cycle,
        max(cycle.reserved_tokens - int(held_tokens), 0),
        max(cycle.reserved_cost_usd - Decimal(held_cost), Decimal(0)),
    )
    cycle.status = status
    cycle.stop_reason = reason[:2000] if reason else None
    cycle.closed_at = datetime.now(UTC)
    await db.flush()


async def close_stale_cycles(db: AsyncSession, *, older_than: timedelta) -> int:
    """Close every cycle still open after *older_than* (its work died). Flushed; the count."""
    cutoff = datetime.now(UTC) - older_than
    stale = (
        (await db.execute(select(HealthCycle.id).where(HealthCycle.status == "open", HealthCycle.opened_at < cutoff)))
        .scalars()
        .all()
    )
    for cycle_id in stale:
        await close_cycle(db, cycle_id, status="failed", reason="Abandoned: still open past its time.")
    if stale:
        logger.warning("Closed %d abandoned Platform Health cycle(s)", len(stale))
    return len(stale)
