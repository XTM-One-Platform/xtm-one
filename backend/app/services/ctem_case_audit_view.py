# SPDX-License-Identifier: Apache-2.0

"""The audit trail of a CTEM case as one viewer reads it (#1490).

Wraps the pure ``ctem_case_audit`` with the two reads it needs: which actor
ids are people (``users``) and the names of the agents the viewer may see
(``caller_visible_filter``: own, shared with them or a group, company-wide).
Any other agent stays an unnamed ``agent`` actor: a name is never served to a
viewer who could not open that agent elsewhere.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.user import User
from app.services import ctem_case_audit as audit


def _ids(values: list[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in values:
        try:
            out.append(uuid.UUID(value))
        except (TypeError, ValueError):
            continue
    return out


async def audit_trail(db: AsyncSession, history: list[dict[str, Any]], *, viewer_id: uuid.UUID) -> dict[str, Any]:
    """``{history, phases, current_phase, actors}``: the served history annotated for this viewer."""
    actor_ids = _ids(sorted({e.get("actor") for e in history if e.get("actor") and e.get("actor") != "system"}))
    people: set[str] = set()
    agents: dict[str, str] = {}
    if actor_ids:
        people = {str(i) for i in (await db.execute(select(User.id).where(User.id.in_(actor_ids)))).scalars()}
        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids

        groups = await get_user_group_ids(db, viewer_id)
        rows = await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(actor_ids), caller_visible_filter(Agent, viewer_id, groups))
        )
        agents = {str(agent_id): name for agent_id, name in rows.all()}
    annotated = audit.annotate(history, people=people)
    for event in annotated:
        event["actor_name"] = (
            agents.get(event.get("actor") or "") if event.get("actor_type") == audit.ACTOR_AGENT else None
        )
    summary = audit.phases(annotated)
    return {"history": annotated, "phases": summary["phases"], "current_phase": summary["current"]}
