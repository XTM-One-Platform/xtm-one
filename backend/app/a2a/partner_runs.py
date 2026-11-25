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

"""The run history a partner task may read: that partner's own tasks.

Inside a partner task the agent builder acts as an anonymous caller
(``partner_scope``), which reads no run history. One read is lent back,
scoped by the partner principal (exposure and OAuth client) and never by the
run-as identity's reach: the runs the partner's own A2A tasks started, and the
runs those spawned (consults, delegations) through ``parent_run_id``.

The same tree scopes ``list_work_items`` there (``partner_work_items_clause``):
the tickets the partner's tasks filed, so one call reads all their statuses.

Each row names the partner task it answers. The result is shown only for the
run a task maps to, which is the text the partner already received; a spawned
run shows its status and times, never its result, prompt or tool calls. A run
outside the set answers exactly like an unknown id.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CTE, ColumnElement, func, or_, select

from app.a2a.partner_scope import PartnerPrincipal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.assignment import AssignmentRun

#: The longest result ``get_run`` shows; ``list_runs`` shows a line of it.
_RESULT_CAP = 20_000
_LINE_CAP = 200


def _partner_run_tree(partner: PartnerPrincipal) -> CTE:
    """``(run_id, task_id)`` of every run the partner's tasks started or spawned."""
    from app.models.a2a import A2ATask
    from app.models.assignment import AssignmentRun

    roots = (
        select(A2ATask.run_id.label("run_id"), A2ATask.id.label("task_id"))
        # Exposure AND client: the pair every partner task read is scoped to,
        # so two partners on one exposure never see each other's runs.
        .where(
            A2ATask.exposure_id == partner.exposure_id,
            A2ATask.client_id == partner.client_id,
            A2ATask.run_id.is_not(None),
        )
        .cte("partner_run_tree", recursive=True)
    )
    children = select(AssignmentRun.id, roots.c.task_id).join(roots, AssignmentRun.parent_run_id == roots.c.run_id)
    # UNION, not UNION ALL: a run reached twice is listed once, and the walk ends.
    # Recursive CTE: the roots, then every run whose parent is in the tree.
    return roots.union(children)


def partner_work_items_clause(partner: PartnerPrincipal) -> ColumnElement[bool]:
    """The work items the partner's own tasks filed: created by a run of its run tree.

    ``list_work_items`` inside a partner task reads this clause on top of its
    scope, so a partner lists the status of every ticket it filed in one call
    and never an item another partner, or the run-as identity, filed.
    """
    from app.models.work_item import WorkItem

    tree = _partner_run_tree(partner)
    own = WorkItem.origin_run_id.in_(select(tree.c.run_id))
    return or_(own, partner_fleet_work_items_clause(partner))


def partner_fleet_work_items_clause(partner: PartnerPrincipal) -> ColumnElement[bool]:
    """The work items of the exposed agent's home fleet, when its exposure opted in (#2202).

    The fleet population of ``work_item.fleet_work_item_clause`` (owned or
    created by a current home member, or filed in the fleet's workspace), for
    the ONE fleet the exposed agent is a home member of, and only while the
    partner's live exposure carries ``partner_fleet_reads``. Off (the
    default), revoked, or an agent in no fleet: the clause matches nothing,
    so the partner keeps reading its own tickets alone. Resolved in SQL at
    query time, never cached on the task.
    """
    from app.models.a2a import A2AExposure
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
    from app.models.work_item import WorkItem

    fleet_ids = (
        select(FleetMember.fleet_id)
        .join(A2AExposure, A2AExposure.agent_id == FleetMember.agent_id)
        .where(
            A2AExposure.id == partner.exposure_id,
            A2AExposure.partner_fleet_reads.is_(True),
            A2AExposure.revoked_at.is_(None),
            FleetMember.role == FLEET_ROLE_MEMBER,
        )
    )
    members = select(FleetMember.agent_id).where(
        FleetMember.fleet_id.in_(fleet_ids), FleetMember.role == FLEET_ROLE_MEMBER
    )
    workspaces = select(Fleet.workspace_id).where(Fleet.id.in_(fleet_ids))
    return or_(
        WorkItem.owner_agent_id.in_(members),
        WorkItem.created_by_agent_id.in_(members),
        WorkItem.workspace_id.in_(workspaces),
    )


def _trigger(run: AssignmentRun, answered_task: bool) -> str:
    if answered_task:
        return "a2a task"
    payload: dict[str, Any] = run.trigger_payload if isinstance(run.trigger_payload, dict) else {}
    return str(payload.get("delegation_type") or payload.get("trigger_type") or "delegation")


def _when(value: datetime | None) -> str:
    return value.isoformat() if value else "-"


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _LINE_CAP else line[:_LINE_CAP] + "…"


async def _answered(db: AsyncSession, run_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """The runs among *run_ids* a partner task maps to (its answer, not a spawn)."""
    from app.models.a2a import A2ATask

    if not run_ids:
        return set()
    rows = await db.execute(select(A2ATask.run_id).where(A2ATask.run_id.in_(run_ids)))
    return {row[0] for row in rows.fetchall()}


async def _agent_names(db: AsyncSession, agent_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.agent import Agent

    if not agent_ids:
        return {}
    rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
    return {row[0]: row[1] for row in rows.fetchall()}


async def list_partner_runs(
    partner: PartnerPrincipal,
    *,
    agent_id: uuid.UUID | None = None,
    status: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """``list_runs`` inside a partner task: the partner's own runs, newest first."""
    from app.core import database
    from app.models.assignment import AssignmentRun

    tree = _partner_run_tree(partner)
    query = select(AssignmentRun, tree.c.task_id).join(tree, AssignmentRun.id == tree.c.run_id)
    if agent_id is not None:
        query = query.where(AssignmentRun.agent_id == agent_id)
    if status:
        query = query.where(AssignmentRun.status == status)

    async with database.async_session() as db:
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        if not total:
            return "No runs of your tasks yet."
        rows = (
            await db.execute(
                query.order_by(AssignmentRun.created_at.desc(), AssignmentRun.id).limit(limit).offset(offset)
            )
        ).all()
        if not rows:
            return f"No runs on this page (offset {offset} of {total} total). Use a smaller offset."
        answered = await _answered(db, {run.id for run, _ in rows})
        names = await _agent_names(db, {run.agent_id for run, _ in rows if run.agent_id})

    first, last = offset + 1, offset + len(rows)
    parts = [f"Found {total} run(s) of your tasks, showing {first}-{last}, most recent first:\n"]
    for run, task_id in rows:
        is_answer = run.id in answered
        line = (
            f"• `{run.id}` — **{names.get(run.agent_id, 'Unknown agent')}** | task `{task_id}`\n"
            f"  Trigger: {_trigger(run, is_answer)} | Status: {run.status} | "
            f"Started: {_when(run.started_at or run.created_at)} | Ended: {_when(run.completed_at)}"
        )
        if is_answer and run.result_summary:
            line += f"\n  Result: {_one_line(run.result_summary)}"
        parts.append(line)
    if last < total:
        parts.append(f"\n{total - last} more run(s). Call list_runs again with offset={last} for the next page.")
    parts.append("\nUse get_run with a run id for its status, times, task and result.")
    return "\n".join(parts)


async def get_partner_run(partner: PartnerPrincipal, run_id: uuid.UUID) -> str:
    """``get_run`` inside a partner task: one of the partner's own runs, else not found."""
    from app.core import database
    from app.models.assignment import AssignmentRun

    tree = _partner_run_tree(partner)
    async with database.async_session() as db:
        row = (
            await db.execute(
                select(AssignmentRun, tree.c.task_id)
                .join(tree, AssignmentRun.id == tree.c.run_id)
                .where(AssignmentRun.id == run_id)
                .limit(1)
            )
        ).first()
        # Same words as an unknown id: a run outside the partner's set is
        # never confirmed to exist.
        if row is None:
            return f"Run `{run_id}` not found."
        run, task_id = row
        is_answer = bool(await _answered(db, {run.id}))
        names = await _agent_names(db, {run.agent_id} if run.agent_id else set())

    lines = [
        f"Run `{run.id}`",
        f"Agent: **{names.get(run.agent_id, 'Unknown agent')}** (`{run.agent_id}`)",
        f"Partner task: `{task_id}`",
        f"Trigger: {_trigger(run, is_answer)}",
        f"Status: {run.status}",
        f"Created: {_when(run.created_at)} | Started: {_when(run.started_at)} | Ended: {_when(run.completed_at)}",
    ]
    if is_answer and run.result_summary:
        result = run.result_summary
        if len(result) > _RESULT_CAP:
            result = result[:_RESULT_CAP] + "…"
        lines.append(f"\nResult delivered to the task:\n{result}")
    elif not is_answer:
        lines.append("\nSpawned while answering the task: its result went to the run that answered it.")
    return "\n".join(lines)
