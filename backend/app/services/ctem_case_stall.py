# SPDX-License-Identifier: Apache-2.0

"""A CTEM case never waits on nobody (#1736).

The case loop runs on events only: an opening, a reopen, an SLA breach, an
OpenAEV result, a ticket done, a gate answered or a reminder the loop armed. A
loop run that ends on a next step written as prose (no gate item, no timer)
leaves the case with nothing that will ever wake it.

Rules here, no model call, run by the scheduled case sweep
(``ctem_case_loop.sweep``):

- A case waits on a person when a needs_human work item keyed ``ctem:...``
  followed by the case id is open (a gate of the case, any prefix).
- A case waits on time when a ``ctem-loop-reminder:CASE_ID`` timer (or a
  legacy loop prefix) is armed.
- An open case that waits on neither, with no case activity for
  :func:`stall_hours`, is stalled: the sweep writes a ``stalled`` case event and
  raises ``ctem_case.stalled``; the fleet that owns the case re-runs its loop.
  The ``stalled`` event is itself activity, so a case still waiting on nobody
  is raised again one stall window later, never every tick.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, String, cast, exists, literal, or_, select

from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.ctem_case_loop import PendingEvents

#: The platform event a stalled case raises (registered in ``platform_events.PLATFORM_EVENTS``).
CASE_STALLED = "ctem_case.stalled"
#: The case event kind the sweep writes on a stalled case.
STALLED = "stalled"
#: Hours without activity after which an open case that waits on nobody is stalled.
DEFAULT_STALL_HOURS = 6
#: Most stalled cases one sweep raises.
STALL_BATCH = 100
#: Every key a CTEM loop mints starts with it (``builtin_agents.CTEM_KEY_PREFIX``).
GATE_KEY_PREFIX = "ctem:"
#: The timer prefixes of the loop (``builtin_agents.CTEM_LOOP_REMINDER_PREFIX`` and its legacy ones).
REMINDER_PREFIXES: tuple[str, ...] = ("ctem-loop-reminder:", "ctem-gate-watch:", "ctem-loop-watch:")

NOBODY = "nobody"
PERSON = "person"
TIME = "time"


def stall_hours() -> int:
    """``CTEM_CASE_STALL_HOURS`` (at least 1), else :data:`DEFAULT_STALL_HOURS`."""
    try:
        value = int(os.environ.get("CTEM_CASE_STALL_HOURS") or DEFAULT_STALL_HOURS)
    except ValueError:
        value = DEFAULT_STALL_HOURS
    return max(1, value)


def reminder_keys(case_id: str) -> list[str]:
    """The timer keys that keep *case_id* waiting on time."""
    return [prefix + case_id for prefix in REMINDER_PREFIXES]


def waiting_on(case_id: str, gate_keys: Iterable[str], timer_keys: Iterable[str]) -> str:
    """``person`` when an open gate item of the case waits, ``time`` when a reminder is armed, else ``nobody``."""
    if any(key.startswith(GATE_KEY_PREFIX) and key.endswith(case_id) for key in gate_keys):
        return PERSON
    reminders = set(reminder_keys(case_id))
    if any(key in reminders for key in timer_keys):
        return TIME
    return NOBODY


def _waits_on_someone(case_id_text: ColumnElement[str]) -> ColumnElement[bool]:
    """SQL: an open needs_human ``ctem:`` item of the case, or an armed reminder of the case (``waiting_on`` in SQL)."""
    from app.models.platform_timer import TIMER_ARMED, PlatformTimer
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem
    from app.services.ctem_decision_groups import GROUPED_REASON_LEAD

    gated = exists().where(
        # A decision of its own, or one grouped with others of the same kind (#2451).
        or_(WorkItem.needs_human.is_(True), WorkItem.blocked_reason.like(f"{GROUPED_REASON_LEAD}%")),
        WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
        WorkItem.correlation_key.like(f"{GATE_KEY_PREFIX}%"),
        WorkItem.correlation_key.like(literal("%", String) + case_id_text),
    )
    reminded = exists().where(
        PlatformTimer.state == TIMER_ARMED,
        or_(*(PlatformTimer.correlation_key == literal(prefix, String) + case_id_text for prefix in REMINDER_PREFIXES)),
    )
    return or_(gated, reminded)


async def sweep_stalled(
    db: AsyncSession, *, events: PendingEvents | None, now: datetime | None = None, actor: str = "system"
) -> int:
    """Raise ``ctem_case.stalled`` on each open case idle past the window that waits on nobody; how many. The caller commits.

    The gate and reminder exclusion runs in the candidate query, before the batch limit, so open cases idle
    behind a gate or a reminder never fill the batch and hide a case that waits on nobody.
    """
    from app.services.ctem_case_loop import _event, _event_id, case_data

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=stall_hours())
    recent = exists().where(CtemCaseEvent.case_id == CtemCase.id, CtemCaseEvent.at > cutoff)
    candidates = (
        (
            await db.execute(
                select(CtemCase)
                .where(
                    CtemCase.state.in_(OPEN_STATES),
                    CtemCase.updated_at <= cutoff,
                    ~recent,
                    ~_waits_on_someone(cast(CtemCase.id, String)),
                )
                .order_by(CtemCase.updated_at.asc())
                .limit(STALL_BATCH)
            )
        )
        .scalars()
        .all()
    )
    raised = 0
    for case in candidates:
        idle_since = case.updated_at or case.state_since
        db.add(
            _event(
                case.id,
                actor,
                STALLED,
                now,
                to_value=NOBODY,
                reason=f"waits on nobody since {idle_since.isoformat() if idle_since else 'unknown'}: "
                "no open gate, no armed reminder",
            )
        )
        raised += 1
        if events is not None:
            events.add(CASE_STALLED, case_data(case), _event_id(CASE_STALLED, case, now.isoformat()))
            events.frame(case, STALLED, now)
    return raised
