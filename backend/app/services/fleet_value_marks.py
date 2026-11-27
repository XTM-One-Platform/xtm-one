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

"""Since when a fleet's value (ROI) is measured (#2580).

A factory reset of a built-in fleet (:func:`app.services.fleet_reset.reset_builtin_fleet`)
stamps ``fleets.value_measured_since``. From then on every value read of the platform
counts, for that fleet, only what happened at or after the mark: its run, work item and
chat records (``fleet_id`` stamped at write) and the LLM spend of its home member agents.
Nothing is deleted: the records, the traces and the audit stay; they are only left out of
the figures. The rule is applied where the facts are read (one place per kind), so the
fleet view, the fleet's row of the platform breakdown and the platform total never differ:
the platform total excludes the same part (``Figures.reset_fleets`` says how many fleets
are measured since a reset inside the window).

* :func:`counted_clause` is the SQL condition a record query adds (records of no fleet, of a
  fleet never reset, or written at or after its fleet's mark).
* :func:`load_marks` reads the marks, :func:`counts` applies them to one fact in Python.
* :func:`agent_marks` maps each agent whose home fleet carries a mark to that mark (the spend
  of an agent is its home fleet's, ``roi_spend``).

A fleet never reset (mark ``NULL``) reads exactly as before.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


def counted_clause(created_at: Any, fleet_id: Any) -> Any:
    """The SQL condition keeping a record (``created_at`` / ``fleet_id`` columns) its fleet's value counts."""
    from app.models.fleet import Fleet

    pre_mark = exists(
        select(Fleet.id).where(
            Fleet.id == fleet_id,
            Fleet.value_measured_since.is_not(None),
            Fleet.value_measured_since > created_at,
        )
    )
    return or_(fleet_id.is_(None), ~pre_mark)


async def load_marks(db: AsyncSession) -> dict[uuid.UUID, datetime]:
    """Every fleet's mark, by fleet id (fleets never reset are absent)."""
    from app.models.fleet import Fleet

    rows = (
        await db.execute(select(Fleet.id, Fleet.value_measured_since).where(Fleet.value_measured_since.is_not(None)))
    ).all()
    return {fid: _aware(at) for fid, at in rows if at is not None}


def _aware(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at


def counts(fleet_id: uuid.UUID | None, at: datetime | date, marks: Mapping[uuid.UUID, datetime]) -> bool:
    """Whether a fact of *fleet_id* at *at* counts: no fleet, no mark, or at or after the mark.

    A ``date`` (a daily cost fact) counts from the day after the mark's day; the mark's own day
    is read from the traces at the minute (``roi_ledger.ledger_facts``).
    """
    mark = marks.get(fleet_id) if fleet_id is not None else None
    if mark is None:
        return True
    if isinstance(at, datetime):
        return _aware(at) >= mark
    return at > mark.date()


def in_window(mark: datetime | None, window_first: date | None) -> bool:
    """Whether *mark* falls inside a window starting *window_first* (``None``: since the start)."""
    return mark is not None and (window_first is None or mark.date() >= window_first)


async def agent_marks(
    db: AsyncSession, agent_ids: Iterable[uuid.UUID], marks: Mapping[uuid.UUID, datetime]
) -> dict[uuid.UUID, datetime]:
    """The mark of each agent of *agent_ids* whose home fleet carries one."""
    if not marks:
        return {}
    from app.services.fleets import home_fleets_of

    homes = await home_fleets_of(db, agent_ids)
    return {aid: marks[h.fleet_id] for aid, h in homes.items() if h.fleet_id in marks}
