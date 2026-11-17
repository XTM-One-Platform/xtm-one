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

"""The subjects of an ROI declaration: who reads and who changes what an agent, assignment or fleet counts.

The ONE place the rule lives (#10006, slice S2): the REST routes of
``api/v1/roi.py`` and the agent tools of ``builtin_tools/roi.py`` both call
it, so an agent acting for its user is refused exactly what that user is
refused on the API.

* Changing a declaration: an agent and a fleet follow the manage rule of
  their own row (owner, ``config`` grantee); an assignment follows its
  agent's; an administrator changes every subject.
* Reading a declaration: whoever can see the subject (own, shared,
  company-managed); an assignment follows its agent's visibility.

A refusal is a :class:`RoiSubjectError` carrying the HTTP status the API
answers (404 absent or not visible, 403 visible but not manageable, 422 a
malformed id); the tools hand its ``detail`` back to the model.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi_records


class RoiSubjectError(Exception):
    """A refused subject: ``status`` is the HTTP status the API answers, ``detail`` the reason."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_subject_id(subject_id: str) -> uuid.UUID:
    """*subject_id* as a UUID; 422 otherwise."""
    try:
        return uuid.UUID(str(subject_id).strip())
    except ValueError:
        raise RoiSubjectError(422, "subject_id must be a UUID") from None


async def manageable_subject(db: AsyncSession, user: Any, subject_type: str, subject_id: str) -> uuid.UUID:
    """The subject's id once *user* may manage it: 404 when absent, 403 when not theirs."""
    from sqlalchemy import select

    from app.api.v1.helpers import can_manage, get_user_group_ids
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.fleet import Fleet

    sid = parse_subject_id(subject_id)
    entity: Any
    if subject_type == roi_records.SUBJECT_FLEET:
        entity = await db.get(Fleet, sid)
    elif subject_type == roi_records.SUBJECT_AGENT:
        entity = await db.get(Agent, sid)
    else:
        agent_id = (await db.execute(select(Assignment.agent_id).where(Assignment.id == sid))).scalar()
        entity = await db.get(Agent, agent_id) if agent_id is not None else None
    if entity is None:
        raise RoiSubjectError(404, f"No {subject_type} {subject_id}.")
    if user.role != "admin" and not can_manage(user, entity, await get_user_group_ids(db, user.id)):
        raise RoiSubjectError(403, f"You cannot manage this {subject_type}.")
    return sid


async def readable_subject(db: AsyncSession, user: Any, subject_type: str, subject_id: str) -> uuid.UUID:
    """The subject's id once *user* may READ it: 404 when absent or not visible."""
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.fleet import Fleet

    sid = parse_subject_id(subject_id)
    if subject_type == roi_records.SUBJECT_ASSIGNMENT:
        agent_id = (await db.execute(select(Assignment.agent_id).where(Assignment.id == sid))).scalar()
    else:
        agent_id = sid
    model: Any = Fleet if subject_type == roi_records.SUBJECT_FLEET else Agent
    groups = await get_user_group_ids(db, user.id)
    visible = (
        await db.execute(
            select(model.id).where(
                model.id == agent_id,
                user_visible_filter(model, user.id, groups, is_admin=user.role == "admin"),
            )
        )
    ).scalar()
    if agent_id is None or visible is None:
        raise RoiSubjectError(404, f"No {subject_type} {subject_id}.")
    return sid


async def declaration_default(db: AsyncSession, subject_type: str, sid: uuid.UUID) -> dict[str, Any] | None:
    """The declaration a built-in agent ships with (``BUILTIN_ROI_UNITS``), ``None`` for any other subject.

    An administrator's declaration on such an agent is its "Customized" value:
    it wins over the seeded ``x-xtm-roi`` (resolution order of design 3.2), the
    seed sync never touches it, and ``DELETE`` resets the agent to this default.
    """
    if subject_type != roi_records.SUBJECT_AGENT:
        return None
    from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
    from app.models.agent import Agent

    agent = await db.get(Agent, sid)
    if agent is None or not getattr(agent, "is_builtin", False):
        return None
    declaration = BUILTIN_ROI_UNITS.get(getattr(agent, "slug", None) or "")
    if declaration is None:
        return None
    return {
        "unit": declaration.unit,
        "count_mode": roi_records.COUNT_PER_OUTPUT_ITEM,
        "count_path": declaration.count_path,
        "units_per_count": 1,
    }


def declaration_dict(
    row: Any, *, default: dict[str, Any] | None = None, names: dict[uuid.UUID, str] | None = None
) -> dict[str, Any]:
    """The JSON shape of a stored declaration, with its origin (default / customized)."""
    return {
        **roi_records.origin_fields(row, names or {}, has_default=default is not None),
        "default": default,
        "subject_type": row.subject_type,
        "subject_id": str(row.subject_id),
        "unit": row.unit,
        "count_mode": row.count_mode,
        "count_path": row.count_path,
        "units_per_count": row.units_per_count,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def read_declaration(db: AsyncSession, subject_type: str, sid: uuid.UUID) -> dict[str, Any] | None:
    """The subject's declaration as ``GET /roi/declarations`` answers it; ``None`` when it declares none.

    A built-in agent nobody customized answers the declaration it ships with,
    ``origin`` "default".
    """
    row = await roi_records.get_declaration(db, subject_type, sid)
    if row is None:
        default = await declaration_default(db, subject_type, sid)
        if default is None:
            return None
        return {
            **roi_records.origin_fields(None, {}, has_default=True),
            "default": default,
            "subject_type": subject_type,
            "subject_id": str(sid),
            **default,
            "updated_at": None,
        }
    return declaration_dict(
        row,
        default=await declaration_default(db, subject_type, sid),
        names=await roi_records.user_names(db, [row.updated_by_user_id]),
    )
