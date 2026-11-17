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

"""The "From chat, estimated" series of the ROI pages (#10544, slice S4; ``dev-docs/roi-outcomes.md`` section 11).

Reads ``roi_chat_records`` (written by ``services/roi_chat_records.py`` and the
one-off pass of ``services/roi_chat_backfill.py``) and applies the rules of the
pure module ``services/roi_chat.py`` on read:

* a line counts once :func:`roi_chat.is_counted` says so (a use signal, or
  settled); the others are reported as ``pending``, never valued;
* its minutes are :func:`roi_chat.estimate` (low bound by size, minus the
  person's measured time plus the margin, capped, floor 0) unless a person
  corrected it (``adjusted_min``), which wins;
* its value is those hours at the rate of the unit's rate role, priced with
  the report's own pricing (``roi_report.load_pricing``), so a currency or a
  role rate means the same thing on both series.

The series is separate from the automation report by construction: nothing
here reads or writes ``roi_run_records``, so a fleet's totals never move with a
chat record.

Who reads a line: the person it is attributed to (``user_id``) and the
administrators, nobody else (Sam, 15 Nov 2026: a line names a person and a
deliverable, so seeing the agent is not enough). Who corrects a line: the same
people (design 11 "anyone may raise or lower a line" became "the person or an
administrator"). A non-administrator's organization read is the totals and the
counts by kind only (``aggregate_only``): no line, no title, no person. The REST
routes and the agent tools share :func:`load_series`, :func:`readable` and
:func:`apply_correction`, never a rule of their own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi_chat

#: The most lines one read answers.
LINES_MAX = 200
#: The most minutes a correction may set (a working month): a typo never inflates the figure.
CORRECTION_MAX_MIN = 30 * 24 * 60


@dataclass(frozen=True)
class Reader:
    """Who reads the series: the organization (an administrator), or the person's own lines."""

    user_id: uuid.UUID | None
    organization: bool = False


async def reader_for(db: AsyncSession | None, *, user_id: uuid.UUID | None, is_admin: bool) -> Reader:
    """The :class:`Reader` of *user_id*: an administrator reads the organization, anyone else their own lines."""
    del db  # The rule needs no read: seeing an agent never opens its lines (Sam, 15 Nov 2026).
    return Reader(user_id=user_id, organization=bool(is_admin))


def readable(reader: Reader, *, agent_id: uuid.UUID | None, user_id: uuid.UUID | None) -> bool:
    """Whether *reader* reads (and so may correct) a line attributed to *user_id*: theirs, or any for an administrator.

    *agent_id* never opens a line: an agent shared with the reader, or company-managed, is
    used by colleagues whose names and deliverables are not the reader's to see.
    """
    del agent_id
    if reader.organization:
        return True
    return reader.user_id is not None and user_id == reader.user_id


def counted_minutes(figures: roi_chat.ChatEstimate, adjusted_min: float | None) -> float:
    """The minutes a counted line shows: a person's correction, else the estimate."""
    if adjusted_min is not None:
        return max(float(adjusted_min), 0.0)
    return figures.counted_min


def apply_correction(
    row: Any, *, minutes: float | None, user_id: uuid.UUID | None, now: datetime | None = None
) -> None:
    """Set (or with ``None`` drop) a person's correction of a line; ``ValueError`` out of range."""
    if minutes is not None:
        value = float(minutes)
        if value != value or value < 0 or value > CORRECTION_MAX_MIN:  # NaN, negative or absurd
            raise ValueError(f"minutes must be between 0 and {CORRECTION_MAX_MIN}.")
        row.adjusted_min = value
        row.adjusted_by_user_id = user_id
        row.adjusted_at = now or datetime.now(UTC)
        return
    row.adjusted_min = None
    row.adjusted_by_user_id = None
    row.adjusted_at = None


def line_dict(
    row: Any,
    *,
    now: datetime,
    user_min: float,
    baselines: Any,
    rate: Any,
) -> dict[str, Any]:
    """One line of the series: the deliverable, whether it counts, its figures and their computation."""
    figures = roi_chat.estimate(row.kind, row.size, user_min=user_min, baselines=baselines)
    counted = roi_chat.is_counted(row.signals or [], last_edit_at=row.last_edit_at, now=now, deleted_at=row.deleted_at)
    minutes = counted_minutes(figures, row.adjusted_min) if counted else 0.0
    return {
        "id": str(row.id),
        "kind": row.kind,
        "deliverable_id": row.deliverable_id,
        "title": row.title,
        "user_id": str(row.user_id) if row.user_id else None,
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "size": row.size,
        "size_label": roi_chat.SIZE_LABEL[row.kind],
        "signals": list(row.signals or []),
        "counted": counted,
        "last_edit_at": row.last_edit_at.isoformat() if row.last_edit_at else None,
        "deleted": row.deleted_at is not None,
        "backfilled": row.backfilled_at is not None,
        "unit": figures.unit,
        "estimate": {
            "manual_min_low": round(figures.manual_min_low, 1),
            "user_min": round(figures.user_min, 1),
            "subtracted_min": round(figures.subtracted_min, 1),
            "cap_min": figures.cap_min,
            "counted_min": round(figures.counted_min, 1),
        },
        "adjusted_min": row.adjusted_min,
        "adjusted_at": row.adjusted_at.isoformat() if row.adjusted_at else None,
        "minutes": round(minutes, 1),
        "hours": round(minutes / 60.0, 2),
        "value": round(minutes / 60.0 * rate.amount, 2) if rate is not None else None,
    }


async def load_series(
    db: AsyncSession,
    *,
    reader: Reader,
    window: str = "30d",
    currency: str | None = None,
    agent_id: uuid.UUID | None = None,
    agent_ids: frozenset[uuid.UUID] | None = None,
    fleet_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
    aggregate_only: bool = False,
) -> dict[str, Any]:
    """The "From chat, estimated" series of *window* as *reader* reads it: totals, by kind, a page of lines.

    The totals cover every readable line of the window; ``lines`` is one page,
    newest edit first. ``agent_id`` narrows it to one agent's lines,
    ``agent_ids`` to the lines of a set of agents. ``fleet_id`` narrows to the lines
    the ledger attributes to that fleet: the fleet stamped on the record when it was
    written (``roi_chat_records.fleet_id``, design 12.3), never today's members, so the
    fleet view's chat card counts what its hero and By fleet count (#12077).
    ``aggregate_only`` answers the totals and the counts by kind with no line
    (a non-administrator's organization read): no title, person or agent leaves.
    """
    from app.models.roi import RoiChatRecord
    from app.services import roi_report
    from app.services.roi_chat_backfill import user_minutes

    now = now or datetime.now(UTC)
    since = roi_report.window_since(window, now=now)
    query = select(RoiChatRecord).order_by(RoiChatRecord.last_edit_at.desc(), RoiChatRecord.id)
    if since is not None:
        query = query.where(RoiChatRecord.created_at >= since)
    if agent_id is not None:
        query = query.where(RoiChatRecord.agent_id == agent_id)
    if agent_ids is not None:
        query = query.where(RoiChatRecord.agent_id.in_(list(agent_ids)))
    if fleet_id is not None:
        query = query.where(RoiChatRecord.fleet_id == fleet_id)
    rows = [
        r for r in (await db.execute(query)).scalars().all() if readable(reader, agent_id=r.agent_id, user_id=r.user_id)
    ]
    pricing = await roi_report.load_pricing(db, currency)
    minutes_by_conversation = await user_minutes(db, {r.conversation_id for r in rows if r.conversation_id})
    lines = []
    for row in rows:
        unit = roi_chat.UNIT_BY_KIND[row.kind]
        baseline = pricing.baselines.get(unit)
        role = baseline.rate_role if baseline is not None else ""
        rate = (pricing.rates.get(role) or {}).get(pricing.currency)
        lines.append(
            line_dict(
                row,
                now=now,
                user_min=minutes_by_conversation.get(row.conversation_id, 0.0),
                baselines=pricing.baselines,
                rate=rate,
            )
        )
    counted = [line for line in lines if line["counted"]]
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in roi_chat.KINDS:
        of_kind = [line for line in counted if line["kind"] == kind]
        by_kind[kind] = {
            "deliverables": len(of_kind),
            "minutes": round(sum(line["minutes"] for line in of_kind), 1),
        }
    total_minutes = sum(line["minutes"] for line in counted)
    values = [line["value"] for line in counted if line["value"] is not None]
    size = max(1, min(int(limit), LINES_MAX))
    skip = max(0, int(offset))
    page = [] if aggregate_only else lines[skip : skip + size]
    for line in page:
        # Every line answered is the reader's to correct (readable), said per line for the UI.
        line["can_adjust"] = readable(reader, agent_id=None, user_id=_uuid_or_none(line["user_id"]))
    await _attach_names(db, page)
    return {
        "label": roi_chat.SERIES_LABEL,
        "window": window,
        "currency": pricing.currency,
        "rate_label": pricing.rate_label,
        "totals": {
            "deliverables": len(counted),
            "pending": len(lines) - len(counted),
            "corrected": sum(1 for line in counted if line["adjusted_min"] is not None),
            "minutes": round(total_minutes, 1),
            "hours": round(total_minutes / 60.0, 2),
            "value": round(sum(values), 2),
        },
        "by_kind": by_kind,
        "aggregate_only": aggregate_only,
        "lines": page,
        "total_lines": 0 if aggregate_only else len(lines),
        "offset": skip,
        "limit": size,
        "method": (
            "Low bound of the unit's manual minutes scaled by size, minus the person's measured time in the "
            f"conversation (gaps capped at {roi_chat.GAP_CAP_MIN:g} min) plus {int(roi_chat.MARGIN * 100)}%, "
            f"capped, never below 0; counted once used or {roi_chat.SETTLE_DAYS} days after the last edit. "
            "A person's correction replaces the estimate."
        ),
    }


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


async def _attach_names(db: AsyncSession, page: list[dict[str, Any]]) -> None:
    """Name the agent and the person of each line of *page* (S5: a line is attributed to both); ``None`` when gone."""
    from app.models.agent import Agent
    from app.models.user import User

    agent_ids = {uuid.UUID(line["agent_id"]) for line in page if line["agent_id"]}
    user_ids = {uuid.UUID(line["user_id"]) for line in page if line["user_id"]}
    agents: dict[str, str] = {}
    users: dict[str, str] = {}
    if agent_ids:
        rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(list(agent_ids))))
        agents = {str(aid): name for aid, name in rows.all()}
    if user_ids:
        rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(list(user_ids))))
        users = {str(uid): name for uid, name in rows.all()}
    for line in page:
        line["agent_name"] = agents.get(line["agent_id"] or "")
        line["user_name"] = users.get(line["user_id"] or "")


async def get_line(db: AsyncSession, record_id: uuid.UUID) -> Any:
    """The chat record *record_id*, ``None`` when absent."""
    from app.models.roi import RoiChatRecord

    return await db.get(RoiChatRecord, record_id)
