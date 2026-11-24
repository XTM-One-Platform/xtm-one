# SPDX-License-Identifier: Apache-2.0

"""Which fleet runs a CTEM case, and the catch-up of the cases nobody runs (#1390, #1966).

dev-docs/ctem-fleets.md section 14: a fleet declares the trigger paths whose
cases it runs (``Fleet.case_ownership``: ``paths`` null for every path, or a
list; ``key`` the prefix of its case work items). The built-in declarations
are seeded from ``BUILTIN_FLEETS`` (the CTEM loop: every path; Vulnerability
management: ``new_vulnerability``); a fork copies its source's, an
administrator sets one on any other fleet (``PUT /fleets/{id}/case-ownership``).
A declaration counts only while one of the fleet's home members holds an
enabled "Case opened or reopened" automation. Exactly one fleet runs a case:

- :func:`owner_of` is the routing table (pure). Precedence, among the active
  declarations holding the path: a list of paths beats every path; then a
  fleet that is not built-in beats a built-in one (a fork takes over from its
  source); then the oldest fleet, then the token. With no active declaration
  the case belongs to the CTEM loop (inactive: the case page says so).
- :func:`stamp_owners` writes ``owner_fleet`` (the owner token, :func:`token_of`)
  on the case events about to be published; each fleet's "Case opened or
  reopened" automation filters on its own token.
- :func:`catch_up` starts the loop of the open cases their owner never took
  (case events fire once, so a case opened before its fleet was enabled was
  never heard): on fleet enable, pause and reset (the paths a fleet gives
  back are taken by the next owner at once), on an ownership change, and on
  the scheduled case sweep (hourly by default, ``CTEM_CASE_SWEEP_SECONDS``).
  Idempotent (an ``owner_requested`` case event per owner, an open
  ``<key>:case:<id>`` item of ANY declared key blocks a second run), bounded
  (``CATCH_UP_BATCH``), P1 first, then the soonest SLA.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builtin_agents import CTEM_CASE_ASSIGNMENT_NAME, CTEM_LOOP_CASE_FLEET
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
#: The owner token of a fleet that is not built-in: its id (a fork's slug is unique per owner only).
CUSTOM_OWNER_PREFIX = "fleet:"
_SEVERITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
_FAR_PAST = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CaseFleet:
    """One fleet's case declaration, as routing reads it."""

    token: str
    fleet_id: str
    slug: str
    name: str
    paths: tuple[str, ...] | None
    key: str
    is_builtin: bool
    created_at: datetime
    active: bool


def token_of(fleet: Any) -> str:
    """The ``owner_fleet`` value of *fleet*: its slug when built-in, ``fleet:<id>`` otherwise."""
    if getattr(fleet, "is_builtin", False):
        return str(fleet.slug)
    return f"{CUSTOM_OWNER_PREFIX}{fleet.id}"


def listener_filter(token: str) -> dict[str, Any]:
    """The payload filter of the "Case opened or reopened" automation of the fleet holding *token*."""
    return {OWNER_KEY: {"eq": token}}


def parse_declaration(value: Any) -> tuple[tuple[str, ...] | None, str] | None:
    """``(paths, key)`` of a stored ``case_ownership``, ``None`` when unset or unreadable."""
    if not isinstance(value, dict):
        return None
    key = value.get("key")
    if not isinstance(key, str) or not key:
        return None
    paths = value.get("paths")
    if paths is None:
        return None, key
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return None
    return tuple(paths), key


def _rank(fleet: CaseFleet) -> tuple[int, int, datetime, str]:
    created = fleet.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (0 if fleet.paths is not None else 1, 1 if fleet.is_builtin else 0, created or _FAR_PAST, fleet.token)


def owner_fleet(trigger_path: str | None, fleets: Iterable[CaseFleet]) -> CaseFleet | None:
    """The active fleet that runs a case on *trigger_path*, by the precedence of the module docstring."""
    holding = [f for f in fleets if f.active and (f.paths is None or trigger_path in f.paths)]
    return min(holding, key=_rank) if holding else None


def owner_of(trigger_path: str | None, fleets: Iterable[CaseFleet]) -> str:
    """The owner token of a case on *trigger_path*; the CTEM loop's when no active fleet holds it."""
    found = owner_fleet(trigger_path, fleets)
    return found.token if found is not None else CTEM_LOOP_CASE_FLEET


async def case_fleets(db: AsyncSession) -> list[CaseFleet]:
    """Every live fleet declaring case ownership, ``active`` when a home member's case automation is on."""
    from app.models.assignment import Assignment
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

    rows = (
        (await db.execute(select(Fleet).where(Fleet.is_archived.is_(False), Fleet.case_ownership.is_not(None))))
        .scalars()
        .all()
    )
    declared = [(f, parse_declaration(f.case_ownership)) for f in rows]
    declared = [(f, d) for f, d in declared if d is not None]
    if not declared:
        return []
    on = {
        fid
        for (fid,) in (
            await db.execute(
                select(FleetMember.fleet_id)
                .join(Assignment, Assignment.agent_id == FleetMember.agent_id)
                .where(
                    FleetMember.fleet_id.in_([f.id for f, _ in declared]),
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    Assignment.name == CTEM_CASE_ASSIGNMENT_NAME,
                    Assignment.enabled.is_(True),
                )
                .distinct()
            )
        ).all()
    }
    return [
        CaseFleet(
            token=token_of(f),
            fleet_id=str(f.id),
            slug=str(f.slug),
            name=str(f.name),
            paths=decl[0],
            key=decl[1],
            is_builtin=bool(f.is_builtin),
            created_at=f.created_at,
            active=f.id in on,
        )
        for f, decl in declared
    ]


async def _fleets_now() -> list[CaseFleet]:
    from app.core.database import async_session

    async with async_session() as db:
        return await case_fleets(db)


async def stamp_owners(datas: Iterable[MutableMapping[str, Any]], *, fleets: list[CaseFleet] | None = None) -> None:
    """Write ``owner_fleet`` on every case event payload in *datas* that has a ``case_id``. Never raises.

    An unreadable routing leaves the payload unstamped: the CTEM loop's
    filter (``owner_fleet`` is ``ctem-loop`` or absent) still takes it.
    """
    pending = [d for d in datas if d.get("case_id") and OWNER_KEY not in d]
    if not pending:
        return
    if fleets is None:
        try:
            fleets = await _fleets_now()
        except Exception:
            logger.warning("CTEM case owners not computed; the CTEM loop takes the events", exc_info=True)
            return
    for data in pending:
        data[OWNER_KEY] = owner_of(data.get("trigger_path"), fleets)


def _catch_up_event_id(case_id: object, owner: str) -> str:
    digest = hashlib.sha256(f"catch-up\n{case_id}\n{owner}".encode()).hexdigest()[:32]
    return f"ctem-case-{digest}"


def _sort_key(case: CtemCase) -> tuple[int, float]:
    due = case.sla_due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return (_SEVERITY_ORDER.get(case.severity or "", 9), due.timestamp() if due else float("inf"))


async def _open_case_item_ids(db: AsyncSession, case_ids: list[str], case_keys: Iterable[str]) -> set[str]:
    """The case ids (among *case_ids*) that already have an open ``<key>:case:<id>`` item of any declared key."""
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

    keys = {f"{key}:case:{cid}": cid for cid in case_ids for key in set(case_keys)}
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
    """The open cases to hand to their active owner, most urgent first: ``(case, owner token)``.

    A case is left out when no active fleet owns it, when it already has an
    open case item (any fleet's), or when the catch-up already asked that
    owner. *only* narrows to the cases owned by those tokens (the fleet just enabled).
    """
    fleets = await case_fleets(db)
    active = {f.token for f in fleets if f.active}
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
        owner = owner_of(case.trigger_path, fleets)
        if owner not in active or (only is not None and owner not in only):
            continue
        if (str(case.id), owner) in already:
            continue
        picked.append((case, owner))
    taken = await _open_case_item_ids(db, [str(c.id) for c, _ in picked], (f.key for f in fleets))
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


async def catch_up_after_change(fleet: Any) -> int:
    """The catch-up after *fleet*'s case automations or declaration changed; 0 for a fleet declaring none.

    Every owner is caught up, not only *fleet*: the paths a paused fleet gives
    back are taken by the next owner at once, and an enabled fleet takes its own.
    """
    if parse_declaration(getattr(fleet, "case_ownership", None)) is None:
        return 0
    return await catch_up()


async def point_case_listeners(db: AsyncSession, fleet: Any, agent_ids: Iterable[uuid.UUID]) -> int:
    """Point the "Case opened or reopened" automations of *agent_ids* at *fleet*'s owner token.

    A fork's listener copied from its source would hear the source's cases;
    rewritten here, it hears only the cases routed to the fork. Answers how
    many were rewritten. The caller flushes and commits.
    """
    from app.models.assignment import Assignment

    ids = list(agent_ids)
    if not ids:
        return 0
    rows = (
        (
            await db.execute(
                select(Assignment).where(
                    Assignment.agent_id.in_(ids),
                    Assignment.name == CTEM_CASE_ASSIGNMENT_NAME,
                    Assignment.trigger_type == "platform_event",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.trigger_config = {**(row.trigger_config or {}), "filter": listener_filter(token_of(fleet))}
    return len(rows)


async def owner_view(db: AsyncSession, case: CtemCase) -> dict[str, Any]:
    """The case page's owner line: ``{slug, name, active, fleet_id}``; ``active`` false when no active fleet owns it."""
    from app.models.fleet import Fleet

    fleets = await case_fleets(db)
    found = owner_fleet(case.trigger_path, fleets)
    if found is not None:
        return {"slug": found.slug, "name": found.name, "active": True, "fleet_id": found.fleet_id}
    row = (
        await db.execute(
            select(Fleet.id, Fleet.name).where(Fleet.is_builtin.is_(True), Fleet.slug == CTEM_LOOP_CASE_FLEET).limit(1)
        )
    ).first()
    return {
        "slug": CTEM_LOOP_CASE_FLEET,
        "name": row[1] if row else CTEM_LOOP_CASE_FLEET,
        "active": False,
        "fleet_id": str(row[0]) if row else None,
    }
