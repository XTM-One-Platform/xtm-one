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

"""The owner's reads a partner task may make, behind its exposure's opt-in (#2638).

Inside a partner task the builder acts as an anonymous caller
(``partner_scope``): fleets are read only through the home-fleet opt-in of
#2202 (``partner_fleet``) and runs only for the partner's own tasks
(``partner_runs``). An exposure that carries ``partner_owner_reads`` lends
the exposed agent's owner's READ scope back to the task:

- ``list_fleets``, ``get_fleet`` and ``get_fleet_health``: every fleet the
  owner reads (``get_fleet_health`` never applies a fix);
- ``list_runs`` and ``get_run``: the runs the owner sees
  (``run_visible_filter``: own and company-managed, never ``scope=all``);
- the running build, on ``list_fleets`` and ``list_runs``.

The owner is the exposure's run-as identity (``A2AExposure.run_as_user_id``).
Every write stays refused: the fleet suite keeps its three reads alone and the
builder stays anonymous. The opt-in is read at every call, never cached on the
task, and every read it lends writes one audit row naming the partner task.
Off (the default) nothing changes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select

from app.a2a.partner_scope import PartnerPrincipal

logger = logging.getLogger(__name__)

#: The audit actor of a read the opt-in lends (``audit.log_system_audit``).
AUDIT_ACTOR = "a2a-partner-owner-reads"
_RESULT_CAP = 20_000
_LINE_CAP = 200


async def partner_owner_id(partner: PartnerPrincipal) -> uuid.UUID | None:
    """The owner whose read scope the partner borrows, ``None`` unless its live exposure opted in."""
    from app.core import database
    from app.models.a2a import A2AExposure

    query = select(A2AExposure.run_as_user_id).where(
        A2AExposure.id == partner.exposure_id,
        A2AExposure.partner_owner_reads.is_(True),
        A2AExposure.revoked_at.is_(None),
    )
    async with database.async_session() as db:
        return (await db.execute(query)).scalar_one_or_none()


async def audit_partner_read(partner: PartnerPrincipal, owner_id: uuid.UUID, tool: str, target: str = "") -> None:
    """One audit row per lent read, anchored on the owner, naming the partner task.

    A failed write is logged, never raised: the read already answered within
    the opt-in, and the row is the trail of it, not its gate."""
    from app.core import database
    from app.services.audit import log_system_audit

    task = str(partner.task_id) if partner.task_id else None
    try:
        async with database.async_session() as db:
            await log_system_audit(
                db,
                actor=AUDIT_ACTOR,
                anchor_user_id=owner_id,
                action="read",
                entity_type="a2a_task",
                entity_id=partner.task_id,
                entity_name=tool,
                summary=f"A2A partner task {task or 'unknown'} read {tool} with the owner's read scope",
                details={
                    "tool": tool,
                    "target": target or None,
                    "partner_task_id": task,
                    "exposure_id": str(partner.exposure_id),
                    "client_id": partner.client_id,
                },
            )
            await db.commit()
    except Exception:  # the trail never breaks the read
        logger.exception("Could not audit the partner read %s of task %s", tool, task)


def running_build_line() -> str:
    from app.services.platform_builds import running_build

    version, git_sha = running_build()
    return f"Running build: {version}" + (f" (commit {git_sha[:12]})" if git_sha else "") + "."


def _when(value: datetime | None) -> str:
    return value.isoformat() if value else "-"


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _LINE_CAP else line[:_LINE_CAP] + "…"


async def list_owner_runs(
    owner_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    status: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """``list_runs`` with the owner's read scope: own and company-managed runs, newest first."""
    from app.api.v1.helpers import run_visible_filter
    from app.core import database
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun

    query = select(AssignmentRun).where(run_visible_filter(owner_id))
    if agent_id is not None:
        query = query.where(AssignmentRun.agent_id == agent_id)
    if status:
        query = query.where(AssignmentRun.status == status)
    async with database.async_session() as db:
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = (
            (
                await db.execute(
                    query.order_by(AssignmentRun.created_at.desc(), AssignmentRun.id).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        agent_ids = {r.agent_id for r in rows if r.agent_id}
        names = (
            {a: n for a, n in (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()}
            if agent_ids
            else {}
        )
    if not total:
        return "No runs readable with the owner's read scope.\n" + running_build_line()
    if not rows:
        return f"No runs on this page (offset {offset} of {total} total). Use a smaller offset."
    first, last = offset + 1, offset + len(rows)
    parts = [f"Found {total} run(s) with the owner's read scope, showing {first}-{last}, most recent first:\n"]
    for run in rows:
        line = (
            f"• `{run.id}` — **{names.get(run.agent_id, 'Unknown agent')}** | Status: {run.status} | "
            f"Started: {_when(run.started_at or run.created_at)} | Ended: {_when(run.completed_at)}"
        )
        if run.result_summary:
            line += f"\n  Result: {_one_line(run.result_summary)}"
        parts.append(line)
    if last < total:
        parts.append(f"\n{total - last} more run(s). Call list_runs again with offset={last} for the next page.")
    parts.append("\n" + running_build_line())
    return "\n".join(parts)


async def get_owner_run(owner_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """``get_run`` with the owner's read scope; a run outside it answers like an unknown id."""
    from app.api.v1.helpers import run_visible_filter
    from app.core import database
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun

    async with database.async_session() as db:
        run = (
            await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_id, run_visible_filter(owner_id)))
        ).scalar_one_or_none()
        if run is None:
            return f"Run `{run_id}` not found."
        name = (
            (await db.execute(select(Agent.name).where(Agent.id == run.agent_id))).scalar_one_or_none()
            if run.agent_id
            else None
        )
    lines = [
        f"Run `{run.id}`",
        f"Agent: **{name or 'Unknown agent'}** (`{run.agent_id}`)",
        f"Status: {run.status}",
        f"Created: {_when(run.created_at)} | Started: {_when(run.started_at)} | Ended: {_when(run.completed_at)}",
    ]
    if run.error:
        lines.append(f"Error: {_one_line(run.error)}")
    if run.result_summary:
        result = run.result_summary
        lines.append("\nResult:\n" + (result[:_RESULT_CAP] + "…" if len(result) > _RESULT_CAP else result))
    return "\n".join(lines)
