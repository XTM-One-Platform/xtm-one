# SPDX-License-Identifier: Apache-2.0

"""Which fleet runs a CTEM case, and the catch-up of the cases nobody runs (#1390).

dev-docs/ctem-fleets.md section 14 (CTEM-31): the CTEM loop owns every case,
on every trigger path. A use-case fleet (``CTEM_CASE_FLEETS`` with ``paths``)
takes the cases of its own paths only while its case automation is enabled;
otherwise the loop takes them. Exactly one fleet runs a case:

- :func:`owner_of` is the routing table (pure).
- :func:`stamp_owners` writes ``owner_fleet`` on the case events about to be
  published; each fleet's "Case opened or reopened" automation filters on it.
- :func:`catch_up` starts the loop of the open cases their owner never took
  (case events fire once, so a case opened before its fleet was enabled was
  never heard): on fleet enable and on the scheduled case sweep (hourly by
  default, ``CTEM_CASE_SWEEP_SECONDS``), so an enable made through a tool is
  caught up within the hour and a daily pass is a floor. Idempotent (an
  ``owner_requested`` case event per owner, an open ``<key>:case:<id>`` item),
  bounded (``CATCH_UP_BATCH``), P1 first, then the soonest SLA.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builtin_agents import (
    CTEM_CASE_ASSIGNMENT_NAME,
    CTEM_CASE_FLEETS,
    CTEM_LOOP_CASE_FLEET,
    CTEM_USE_CASE_FLEETS,
)
from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent

logger = logging.getLogger(__name__)

#: The payload key a case event carries its owner under (a filter key of every case event).
OWNER_KEY = "owner_fleet"
#: The case event recording that the catch-up asked the owner to run the case.
OWNER_REQUESTED = "owner_requested"
#: Most cases one catch-up pass starts.
CATCH_UP_BATCH = 100
#: Most open cases one pass reads (the most urgent first).
SCAN_MAX = 2000
_SEVERITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def owner_of(trigger_path: str | None, active: Iterable[str]) -> str:
    """The fleet slug that runs a case on *trigger_path*, given the *active* use-case fleets.

    The first active use-case fleet declaring the path, else the CTEM loop.
    """
    on = set(active)
    for slug in CTEM_USE_CASE_FLEETS:
        if slug in on and trigger_path in (CTEM_CASE_FLEETS[slug]["paths"] or ()):
            return slug
    return CTEM_LOOP_CASE_FLEET


async def active_case_fleets(db: AsyncSession) -> set[str]:
    """The fleet slugs whose "Case opened or reopened" automation is enabled (by orchestrator slug)."""
    from app.models.agent import Agent
    from app.models.assignment import Assignment

    by_agent = {spec["agent"]: slug for slug, spec in CTEM_CASE_FLEETS.items()}
    rows = (
        await db.execute(
            select(Agent.slug)
            .join(Assignment, Assignment.agent_id == Agent.id)
            .where(
                Agent.slug.in_(sorted(by_agent)),
                Assignment.name == CTEM_CASE_ASSIGNMENT_NAME,
                Assignment.enabled.is_(True),
            )
        )
    ).all()
    return {by_agent[slug] for (slug,) in rows if slug in by_agent}


async def _active_now() -> set[str]:
    from app.core.database import async_session

    async with async_session() as db:
        return await active_case_fleets(db)


async def stamp_owners(datas: Iterable[MutableMapping[str, Any]], *, active: set[str] | None = None) -> None:
    """Write ``owner_fleet`` on every case event payload in *datas* that has a ``case_id``. Never raises.

    An unreadable activation leaves the payload unstamped: the CTEM loop's
    filter (``owner_fleet`` not a use-case fleet) still takes it.
    """
    pending = [d for d in datas if d.get("case_id") and OWNER_KEY not in d]
    if not pending:
        return
    if active is None:
        try:
            active = await _active_now()
        except Exception:
            logger.warning("CTEM case owners not computed; the CTEM loop takes the events", exc_info=True)
            return
    for data in pending:
        data[OWNER_KEY] = owner_of(data.get("trigger_path"), active)


def _catch_up_event_id(case_id: object, owner: str) -> str:
    digest = hashlib.sha256(f"catch-up\n{case_id}\n{owner}".encode()).hexdigest()[:32]
    return f"ctem-case-{digest}"


def _sort_key(case: CtemCase) -> tuple[int, float]:
    due = case.sla_due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return (_SEVERITY_ORDER.get(case.severity or "", 9), due.timestamp() if due else float("inf"))


async def _open_case_item_ids(db: AsyncSession, case_ids: list[str]) -> set[str]:
    """The case ids (among *case_ids*) that already have an open ``<fleet key>:case:<id>`` work item."""
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

    keys = {f"{spec['key']}:case:{cid}": cid for cid in case_ids for spec in CTEM_CASE_FLEETS.values()}
    if not keys:
        return set()
    rows = (
        await db.execute(
            select(WorkItem.correlation_key).where(
                WorkItem.correlation_key.in_(sorted(keys)),
                WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
            )
        )
    ).all()
    return {keys[key] for (key,) in rows if key in keys}


async def plan_catch_up(
    db: AsyncSession, *, only: set[str] | None = None, limit: int = CATCH_UP_BATCH
) -> list[tuple[CtemCase, str]]:
    """The open cases to hand to their active owner, most urgent first: ``(case, owner)``.

    A case is left out when no active fleet owns it, when it already has an
    open case item, or when the catch-up already asked that owner. *only*
    narrows to the cases owned by those fleets (the fleet just enabled).
    """
    active = await active_case_fleets(db)
    if not active:
        return []
    # P1 before P2 before P3 (the labels sort that way), then the soonest SLA; bounded read.
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.state.in_(OPEN_STATES))
                .order_by(CtemCase.severity.asc().nulls_last(), CtemCase.sla_due_at.asc().nulls_last())
                .limit(SCAN_MAX)
            )
        )
        .scalars()
        .all()
    )
    if not cases:
        return []
    asked_rows = (
        await db.execute(
            select(CtemCaseEvent.case_id, CtemCaseEvent.to_value).where(
                CtemCaseEvent.kind == OWNER_REQUESTED,
                CtemCaseEvent.case_id.in_([c.id for c in cases]),
            )
        )
    ).all()
    already = {(str(cid), owner) for cid, owner in asked_rows}
    picked: list[tuple[CtemCase, str]] = []
    for case in sorted(cases, key=_sort_key):
        owner = owner_of(case.trigger_path, active)
        if owner not in active or (only is not None and owner not in only):
            continue
        if (str(case.id), owner) in already:
            continue
        picked.append((case, owner))
    taken = await _open_case_item_ids(db, [str(c.id) for c, _ in picked])
    return [(c, o) for c, o in picked if str(c.id) not in taken][:limit]


async def catch_up(*, only: set[str] | None = None, now: datetime | None = None) -> int:
    """Hand every open case its owner never ran to that owner: one ``ctem_case.opened`` each. Never raises.

    Runs in its own session after the caller's commit (the enabled
    automation must be visible to the dispatcher). Answers how many were raised.
    """
    from app.core.database import async_session
    from app.services import ctem_case_loop as loop
    from app.services.platform_events import publish

    now = now or datetime.now(UTC)
    try:
        async with async_session() as db:
            planned = await plan_catch_up(db, only=only)
            if not planned:
                return 0
            out: list[tuple[dict[str, Any], str]] = []
            for case, owner in planned:
                db.add(
                    CtemCaseEvent(
                        case_id=case.id,
                        actor="system",
                        kind=OWNER_REQUESTED,
                        at=now,
                        to_value=owner,
                        reason="catch-up",
                    )
                )
                out.append(({**loop.case_data(case), OWNER_KEY: owner}, _catch_up_event_id(case.id, owner)))
            await db.commit()
        raised = 0
        for data, event_id in out:
            if await publish(loop.CASE_OPENED, data, event_id=event_id):
                raised += 1
        if raised:
            logger.info("CTEM case catch-up: %d case(s) handed to their fleet", raised)
        return raised
    except Exception:
        logger.warning("CTEM case catch-up failed; the next pass retries", exc_info=True)
        return 0


async def catch_up_after_enable(fleet_slug: str | None) -> int:
    """The catch-up of the fleet just enabled (a CTEM case fleet only); 0 for any other fleet."""
    if fleet_slug not in CTEM_CASE_FLEETS:
        return 0
    return await catch_up(only={fleet_slug})


async def owner_view(db: AsyncSession, case: CtemCase) -> dict[str, Any]:
    """The case page's owner line: ``{slug, name, active}``; ``active`` false when no active fleet owns it."""
    from app.models.fleet import Fleet

    active = await active_case_fleets(db)
    slug = owner_of(case.trigger_path, active)
    name = (await db.execute(select(Fleet.name).where(Fleet.slug == slug).limit(1))).scalar_one_or_none()
    return {"slug": slug, "name": name or slug, "active": slug in active}
