# SPDX-License-Identifier: Apache-2.0

"""The gate decision records of a CTEM case as one viewer reads them (#1518).

Wraps the pure ``ctem_case_gate_records`` with its reads: the gate work items of the
case the viewer may read (``work_item.list_work_items``, the viewer's own
work-item visibility, never a wider one), their ``decisions`` rows, and the
names of the people, groups and agents those rows name. A decision of an item
the viewer may not read is not served; an agent the viewer could not open
elsewhere stays unnamed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.decision import Decision
from app.models.group import Group
from app.models.user import User
from app.services import ctem_case_gate_records as gates


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _row(decision: Decision) -> dict[str, Any]:
    return {
        "id": str(decision.id),
        "public_id": decision.public_id,
        "status": decision.status,
        "ask": decision.ask or {},
        "recipient": decision.recipient,
        "answer": decision.answer,
        "answered_by": str(decision.answered_by) if decision.answered_by else None,
        "answered_at": decision.answered_at,
        "surface": decision.surface,
        "deadline": decision.deadline,
        "created_at": decision.created_at,
    }


async def _gate_items(case_id: str, viewer_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.services import work_item as work_item_service

    found: list[dict[str, Any]] = []
    for key, gate in gates.item_keys(case_id).items():
        items = await work_item_service.list_work_items(user_id=str(viewer_id), correlation_key=key, limit=5)
        found.extend({**item, "_gate": gate} for item in items)
    return found


async def _names(db: AsyncSession, ids: dict[str, set[str]], viewer_id: uuid.UUID) -> dict[str, str]:
    names: dict[str, str] = {}
    users = [u for u in (_uuid(i) for i in ids["user"]) if u]
    if users:
        for user_id, full_name, email in (
            await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(users)))
        ).all():
            names[str(user_id)] = full_name or email
    groups = [g for g in (_uuid(i) for i in ids["group"]) if g]
    if groups:
        for group_id, name in (await db.execute(select(Group.id, Group.name).where(Group.id.in_(groups)))).all():
            names[str(group_id)] = name
    agents = [a for a in (_uuid(i) for i in ids["agent"]) if a]
    if agents:
        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids

        viewer_groups = await get_user_group_ids(db, viewer_id)
        rows = await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.id.in_(agents), caller_visible_filter(Agent, viewer_id, viewer_groups)
            )
        )
        names.update({str(agent_id): name for agent_id, name in rows.all()})
    return names


async def gate_records(
    db: AsyncSession, case_id: str, history: list[dict[str, Any]], *, viewer_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The case's gate records for this viewer; ``answer`` of the waiting gate events is extended in place."""
    items = await _gate_items(case_id, viewer_id)
    by_id = {i["id"]: i for i in items if _uuid(i.get("id"))}
    decisions: list[dict[str, Any]] = []
    if by_id:
        rows = (
            await db.execute(
                select(Decision).where(
                    Decision.source_type == "work_item",
                    Decision.source_id.in_([_uuid(i) for i in by_id]),
                )
            )
        ).scalars()
        for decision in rows:
            item = by_id.get(str(decision.source_id)) or {}
            decisions.append(
                {
                    "gate": item.get("_gate"),
                    "work_item_id": item.get("id"),
                    "created_by_agent_id": item.get("created_by_agent_id"),
                    "decision": _row(decision),
                }
            )
    ids: dict[str, set[str]] = {"user": set(), "group": set(), "agent": set()}
    for entry in decisions:
        row = entry["decision"]
        if row.get("answered_by"):
            ids["user"].add(row["answered_by"])
        kind, _, ident = (row.get("recipient") or "").partition(":")
        if kind in ("user", "group") and ident:
            ids[kind].add(ident)
        if entry.get("created_by_agent_id"):
            ids["agent"].add(entry["created_by_agent_id"])
    for event in history:
        if event.get("kind") == "gate" and event.get("to") == "answered" and event.get("reason"):
            ids["user"].add(event["reason"])
        if event.get("kind") == "gate" and event.get("actor_type") == "agent" and event.get("actor"):
            ids["agent"].add(event["actor"])
    names = await _names(db, ids, viewer_id)
    records = gates.resolve(history, decisions, names=names)
    gates.attach(history, records)
    return records
