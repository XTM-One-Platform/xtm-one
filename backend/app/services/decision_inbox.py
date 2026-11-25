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

"""The "Waiting for you" inbox: every open decision of a person, whatever asked it (#842, S5).

``dev-docs/human-gates.md`` section 4 (C): one list across the four gates (a
run's question, a tool approval, a flow Ask-user park, a work item), oldest
deadline first then oldest asked, one indexed read of ``decisions`` on
(``recipient``, ``status``, ``deadline``) and no model call. The companion
inbox and the home notice read the same list.

The caller's recipients follow the one recipient rule of section 6: the
decisions addressed to them (``user:<id>``), to one of their groups
(``group:<id>``), to every member (``company``) and, for an administrator, to
the administrators (``admins``).

The list is served only while the read switch is on (section 8, migration
contract step 3): the setting AND a complete backfill count check. The count
check walks every open gate, so its verdict is kept :data:`SWITCH_TTL_SECONDS`
per process; the answer says ``enabled: false`` with the reason otherwise and
the readers keep their per-engine views.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import cast
from sqlalchemy.types import Text

from app.core.json_sql import when_json_parseable
from app.models.decision import Decision, public_id

#: Seconds a read switch verdict is reused by this process.
SWITCH_TTL_SECONDS = 60.0
#: The most rows one read answers.
MAX_LIMIT = 200

_switch_cache: dict[str, Any] = {}


@dataclass(frozen=True)
class SourceNames:
    """What a row shows of its source: where it comes from, the asking agent, the flow a link needs."""

    origin: str | None = None
    agent: str | None = None
    flow_id: uuid.UUID | None = None


async def inbox_switch(db: AsyncSession, *, now: float | None = None) -> dict[str, Any]:
    """The read switch (``decision_backfill.read_switch``), its verdict reused for a minute."""
    from app.core.config import settings
    from app.services.decision_backfill import read_switch

    if not settings.decisions_read_enabled:
        return {"on": False, "reason": "DECISIONS_READ_ENABLED is off"}
    clock = time.monotonic() if now is None else now
    cached = _switch_cache.get("verdict")
    if cached is not None and clock - _switch_cache.get("at", 0.0) < SWITCH_TTL_SECONDS:
        return dict(cached)
    conn = await db.connection()
    verdict = await conn.run_sync(lambda sync_conn: read_switch(sync_conn, enabled=True))
    answer = {"on": bool(verdict.get("on")), "reason": verdict.get("reason")}
    _switch_cache.update(verdict=answer, at=clock)
    return answer


async def recipients_of(db: AsyncSession, user: Any) -> list[str]:
    """The recipient keys a decision may name to reach *user* (section 6)."""
    from app.api.v1.helpers import get_user_group_ids

    keys = [f"user:{user.id}", "company"]
    keys += [f"group:{group_id}" for group_id in await get_user_group_ids(db, user.id)]
    if getattr(user, "is_admin", False):
        keys.append("admins")
    return keys


async def _source_names(db: AsyncSession, rows: list[Decision]) -> dict[tuple[str, uuid.UUID], SourceNames]:
    """One read per source type: the assignment, flow or item a row comes from and its agent."""
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.flow import Flow
    from app.models.flow_run import FlowRun
    from app.models.work_item import WorkItem

    by_type: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        by_type.setdefault(row.source_type, []).append(row.source_id)
    names: dict[tuple[str, uuid.UUID], SourceNames] = {}
    if ids := by_type.get("run"):
        result = await db.execute(
            select(AssignmentRun.id, Assignment.name, Agent.name)
            .join(Agent, Agent.id == AssignmentRun.agent_id)
            .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(AssignmentRun.id.in_(ids))
        )
        for run_id, assignment_name, agent_name in result.all():
            names[("run", run_id)] = SourceNames(origin=assignment_name or agent_name, agent=agent_name)
    if ids := by_type.get("flow_run"):
        result = await db.execute(
            select(FlowRun.id, Flow.id, Flow.name).join(Flow, Flow.id == FlowRun.flow_id).where(FlowRun.id.in_(ids))
        )
        for run_id, flow_id, flow_name in result.all():
            names[("flow_run", run_id)] = SourceNames(origin=flow_name, flow_id=flow_id)
    if ids := by_type.get("work_item"):
        result = await db.execute(
            select(WorkItem.id, WorkItem.title, Agent.name)
            .outerjoin(Agent, Agent.id == WorkItem.owner_agent_id)
            .where(WorkItem.id.in_(ids))
        )
        for item_id, title, agent_name in result.all():
            names[("work_item", item_id)] = SourceNames(origin=title, agent=agent_name)
    return names


def fleet_decisions_clause(fleet_id: uuid.UUID):
    """The open decisions a fleet raised: sources of its current home members (#1493).

    A run of a home member (its ``agent_id``) and a work item a home member owns
    or created. A flow run is not listed: a flow has no agent column to tie it
    to a member. Resolved when the query runs, so a row follows
    the agents in the fleet now, the population of the fleet's other lists
    (:func:`app.services.fleet_views.fleet_scope_clause`).
    """
    from sqlalchemy import and_, or_

    from app.models.assignment import AssignmentRun
    from app.models.work_item import WorkItem
    from app.services.fleet_views import fleet_scope_clause

    runs = select(AssignmentRun.id).where(fleet_scope_clause(AssignmentRun.agent_id, fleet_id))
    items = select(WorkItem.id).where(
        or_(
            fleet_scope_clause(WorkItem.owner_agent_id, fleet_id),
            fleet_scope_clause(WorkItem.created_by_agent_id, fleet_id),
        )
    )
    return or_(
        and_(Decision.source_type == "run", Decision.source_id.in_(runs)),
        and_(Decision.source_type == "work_item", Decision.source_id.in_(items)),
    )


def present(row: Decision, names: SourceNames, *, can_answer: bool = True) -> dict[str, Any]:
    """One inbox row: the columns of section 4 and what the card and the link need."""
    from app.services.decision_links import decision_link

    ask = row.ask if isinstance(row.ask, dict) else {}
    return {
        "id": public_id(row.source_type, row.source_id),
        "source_type": row.source_type,
        "source_id": str(row.source_id),
        "kind": row.kind,
        "question": str(ask.get("question") or ""),
        "ask": ask,
        "origin": names.origin,
        "agent": names.agent,
        "asked_at": row.created_at.isoformat() if row.created_at else None,
        "deadline": row.deadline.isoformat() if row.deadline else None,
        "link": decision_link(row.source_type, row.source_id, flow_id=names.flow_id),
        # False on a fleet read for a decision addressed to someone else: shown read only (#1493).
        "can_answer": can_answer,
    }


def _reaches(row: Any, keys: list[str], groups: list[str]) -> bool:
    """Whether a decision row is addressed to the caller: its recipient key, or one of its groups (#1748)."""
    if row.recipient in keys:
        return True
    listed = (row.ask or {}).get("recipient_groups") if isinstance(row.ask, dict) else None
    return bool(listed) and any(str(g) in groups for g in listed)


async def waiting_for(
    db: AsyncSession,
    user: Any,
    *,
    limit: int = 50,
    fleet_id: uuid.UUID | None = None,
    every_recipient: bool = False,
) -> dict[str, Any]:
    """The open decisions *user* may answer, soonest deadline first, then oldest asked.

    With *fleet_id*, only the decisions that fleet raised (:func:`fleet_decisions_clause`,
    the fleet's Approval tab, #1493). *every_recipient* (the caller manages the fleet,
    checked by the route) also lists the fleet's decisions addressed to someone else,
    each marked ``can_answer: false``: shown read only, never hidden silently.
    """
    switch = await inbox_switch(db)
    if not switch["on"]:
        return {"enabled": False, "reason": switch["reason"], "items": []}
    keys = await recipients_of(db, user)
    query = select(Decision).where(Decision.status == "open")
    if fleet_id is not None:
        query = query.where(fleet_decisions_clause(fleet_id))
    groups = [k.partition(":")[2] for k in keys if k.startswith("group:")]
    reach = Decision.recipient.in_(keys)
    if groups:
        # A decision addressed to several groups names them all in its ask (#1748).
        reach = or_(
            reach,
            Decision.recipient.like("group:%")
            # A row whose ask PostgreSQL cannot parse is left out, never aborting the inbox.
            & when_json_parseable(
                Decision.ask,
                cast(Decision.ask, JSONB)["recipient_groups"].has_any(cast(groups, ARRAY(Text))),
            ),
        )
    if fleet_id is None or not every_recipient:
        query = query.where(reach)
    rows = list(
        (
            await db.execute(
                query.order_by(Decision.deadline.asc().nulls_last(), Decision.created_at.asc()).limit(
                    max(1, min(limit, MAX_LIMIT))
                )
            )
        ).scalars()
    )
    names = await _source_names(db, rows)
    items = [
        present(row, names.get((row.source_type, row.source_id), SourceNames()), can_answer=_reaches(row, keys, groups))
        for row in rows
    ]
    return {"enabled": True, "reason": None, "items": items}
