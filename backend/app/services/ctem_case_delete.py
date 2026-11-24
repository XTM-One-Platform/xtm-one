# SPDX-License-Identifier: Apache-2.0

"""Delete CTEM cases, one or many, administrators only (#1723).

A delete removes the case and everything tied to it, so its fix key is free and
the next case for that key opens fresh, with no old history:

- its members and history (``ctem_case_issues``, ``ctem_case_events``: ``ON DELETE CASCADE``);
- its open work items (gates, loop steps, fleet case items, re-tests): every
  non-terminal item whose correlation key names the case id is cancelled with
  a result that says why;
- its armed timers (loop reminders, gate and loop watches): every armed timer
  whose correlation key names the case id is cancelled;
- its signals: a signal that opened or joined the case goes back to the scope
  gate (state ``new``, decision cleared, decided again), so the scheduled case
  sweep regroups it on its fix key.

A case UUID names one case only, so a correlation key that holds it belongs to
that case whatever the fleet or step that wrote it: no key list to keep in step.

One audit row per case (who, when, reason, and the case summary, kept in the
audit row since the case is gone). ``dry_run`` answers the same summary and
writes nothing. The source issues of a case built by a recompute (OpenCTI,
OpenAEV) are not recreated by a recompute alone: recompute never builds a case
(#1529); they come back through their signals and the gate.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue

logger = logging.getLogger(__name__)

#: Most cases one delete names or one filter selects.
MAX_CASES = 200
#: The ``opened_by`` filter value of a case built by a source read before signals.
OPENED_BY_LEGACY = "legacy"
#: The result written on a work item a case delete cancelled.
CANCELLED_ITEM_RESULT = "Cancelled: CTEM case {case_id} was deleted by an administrator ({actor}). {reason}"
#: The outcome written on a timer a case delete cancelled.
CANCELLED_TIMER_OUTCOME = "cancelled: CTEM case {case_id} deleted"


def origin(events: Iterable[Any]) -> str:
    """How a case came to be, from its history: ``signal``, ``split`` or ``legacy``.

    The same reading as the Cases page (``case-loop-reason.ts`` ``noSignalOrigin``):
    a case split by a person or an agent has a ``created`` event to ``manual``; a
    case a signal opened or joined names it; a case with neither was built by a
    source read before the readers recorded signals.
    """
    rows = list(events)
    if any(getattr(e, "kind", None) == "created" and getattr(e, "to_value", None) == "manual" for e in rows):
        return "split"
    for e in rows:
        kind = getattr(e, "kind", None)
        if kind == "signal" or (kind == "created" and (getattr(e, "reason", None) or "").startswith("signal ")):
            return "signal"
    return OPENED_BY_LEGACY


@dataclass
class CaseDeletion:
    """What one case delete removes (also the dry-run answer)."""

    id: str
    title: str
    state: str
    keys: list[str]
    opened_by: str
    severity: str | None
    created_at: str | None
    members: int
    events: int
    work_items: list[str] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "keys": self.keys,
            "opened_by": self.opened_by,
            "severity": self.severity,
            "created_at": self.created_at,
            "members": self.members,
            "events": self.events,
            "work_items": self.work_items,
            "timers": self.timers,
            "signals": self.signals,
        }


async def select_cases(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    case_ids: Iterable[uuid.UUID] = (),
    states: Iterable[str] = (),
    opened_by: str | None = None,
    vertical: str | None = None,
) -> tuple[list[CtemCase], list[str]]:
    """The cases a delete names (ids) or selects (a filter); ``(cases, unknown ids)``.

    Ids and a filter combine: a named case must also match the filter. At most
    :data:`MAX_CASES` cases are answered (the oldest first).
    """
    query = select(CtemCase).where(CtemCase.organization_id == organization_id)
    wanted = list(dict.fromkeys(case_ids))
    if wanted:
        query = query.where(CtemCase.id.in_(wanted))
    state_list = [s for s in states if s]
    if state_list:
        query = query.where(CtemCase.state.in_(state_list))
    if vertical:
        query = query.where(CtemCase.vertical == vertical)
    cases = list((await db.execute(query.order_by(CtemCase.created_at.asc()))).scalars())
    if opened_by:
        history = await _events(db, [c.id for c in cases])
        cases = [c for c in cases if origin(history.get(c.id, [])) == opened_by]
    found = {c.id for c in cases}
    unknown = [str(cid) for cid in wanted if cid not in found]
    return cases[:MAX_CASES], unknown


async def _events(db: AsyncSession, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[CtemCaseEvent]]:
    out: dict[uuid.UUID, list[CtemCaseEvent]] = {cid: [] for cid in case_ids}
    if not case_ids:
        return out
    for row in (await db.execute(select(CtemCaseEvent).where(CtemCaseEvent.case_id.in_(case_ids)))).scalars():
        out.setdefault(row.case_id, []).append(row)
    return out


async def _open_items(db: AsyncSession, case_id: uuid.UUID) -> list[Any]:
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    return list(
        (
            await db.execute(
                select(WorkItem).where(
                    WorkItem.correlation_key.contains(str(case_id)),
                    WorkItem.status.notin_(WORK_ITEM_TERMINAL_STATUSES),
                )
            )
        ).scalars()
    )


async def _armed_timers(db: AsyncSession, case_id: uuid.UUID) -> list[Any]:
    from app.models.platform_timer import TIMER_ARMED, PlatformTimer

    return list(
        (
            await db.execute(
                select(PlatformTimer).where(
                    PlatformTimer.correlation_key.contains(str(case_id)), PlatformTimer.state == TIMER_ARMED
                )
            )
        ).scalars()
    )


async def _signals(db: AsyncSession, case_id: uuid.UUID) -> list[Any]:
    from app.models.ctem_signal import CtemSignal

    return list((await db.execute(select(CtemSignal).where(CtemSignal.case_id == case_id))).scalars())


async def plan(db: AsyncSession, case: CtemCase, history: list[CtemCaseEvent]) -> CaseDeletion:
    """What deleting *case* removes, read without writing."""
    members = (await db.execute(select(CtemCaseIssue.id).where(CtemCaseIssue.case_id == case.id))).all()
    created = getattr(case, "created_at", None)
    return CaseDeletion(
        id=str(case.id),
        title=case.title,
        state=case.state,
        keys=list(case.keys or []),
        opened_by=origin(history),
        severity=getattr(case, "severity", None),
        created_at=created.isoformat() if created else None,
        members=len(members),
        events=len(history),
        work_items=[i.correlation_key for i in await _open_items(db, case.id)],
        timers=[t.correlation_key for t in await _armed_timers(db, case.id)],
        signals=[str(s.id) for s in await _signals(db, case.id)],
    )


async def _release_signals(db: AsyncSession, case_id: uuid.UUID, *, now: datetime) -> None:
    """Send the signals of a deleted case back to the scope gate (state ``new``, decided again)."""
    from app.services import ctem_scope_gate_store as gate_store

    for row in await _signals(db, case_id):
        row.case_id = None
        row.state = "new"
        row.decision = None
        row.updated_at = now
        try:
            async with db.begin_nested():
                await gate_store.evaluate_one(db, row, evaluator_id=None, now=now)
        except Exception:
            # It stays new: the on-demand gate pass (POST /ctem-signals/evaluate) decides it.
            logger.warning("CTEM signal %s released from a deleted case, gate not run", row.id, exc_info=True)


async def delete_cases(
    db: AsyncSession,
    cases: list[CtemCase],
    *,
    actor: Any,
    reason: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete *cases* as the administrator *actor* (a ``User``); the caller commits.

    Answers ``{dry_run, count, cases: [summary]}``. A dry run writes nothing.
    """
    from app.services.audit import log_audit

    now = now or datetime.now(UTC)
    history = await _events(db, [c.id for c in cases])
    summaries: list[dict[str, Any]] = []
    actor_name = getattr(actor, "email", None) or str(getattr(actor, "id", "an administrator"))
    note = (reason or "").strip()
    for case in cases:
        deletion = await plan(db, case, history.get(case.id, []))
        summaries.append(deletion.summary())
        if dry_run:
            continue
        for item in await _open_items(db, case.id):
            item.status = "cancelled"
            item.needs_human = False
            item.completed_at = now
            item.result = CANCELLED_ITEM_RESULT.format(case_id=case.id, actor=actor_name, reason=note).strip()
        for timer in await _armed_timers(db, case.id):
            from app.models.platform_timer import TIMER_CANCELLED

            timer.state = TIMER_CANCELLED
            timer.outcome = CANCELLED_TIMER_OUTCOME.format(case_id=case.id)
            timer.ended_at = now
        await _release_signals(db, case.id, now=now)
        await log_audit(
            db,
            user=actor,
            action="delete",
            entity_type="ctem_case",
            entity_id=case.id,
            entity_name=case.title[:255] if case.title else None,
            summary=f"Deleted CTEM case {case.title!r}" + (f": {note}" if note else ""),
            details={"reason": note or None, "case": deletion.summary(), "deleted_at": now.isoformat()},
        )
        await db.delete(case)
    if not dry_run:
        await db.flush()
    return {"dry_run": dry_run, "count": len(summaries), "cases": summaries}
