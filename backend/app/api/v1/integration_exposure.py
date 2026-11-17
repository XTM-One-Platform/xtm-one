# SPDX-License-Identifier: Apache-2.0

"""Who can act with an integration's stored credential, and a preview of what a
change would lend (read-only).

``GET /integrations/{id}/exposure`` lists every path (direct shares, agents,
assignments, flows, knowledge bases, skills, the General Assistant) with its
audience, for someone who can manage the integration. ``GET
/integrations/exposure/summary`` gives the list cards their counts, over the
integrations the caller sees and manages (three-tier, never an admin-wide
list). ``POST /integrations/exposure/preview`` answers "if I save this, whose
credentials become usable by whom", for the confirmation shown before a share,
a binding or an owner change. Every answer is the one exposure engine's
(``app.services.resource_exposure``), in this route's shape
(``app.services.integration_exposure``). Nothing here writes or refuses a
change.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    can_manage,
    get_user_group_ids,
    get_user_integration,
    parse_path_id,
    user_visible_filter,
)
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.flow import Flow
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.integration_exposure import (
    exposure_summaries,
    integration_exposure,
    preview_exposure,
)

exposure_router = APIRouter()

EXPOSURE_FORBIDDEN_DETAIL = (
    "Only people who can manage this integration can see who can use its credentials."
)

_ENTITY_MODELS: dict[str, Any] = {
    "integration": Integration,
    "agent": Agent,
    "flow": Flow,
    "knowledge_base": KnowledgeBase,
    "skill": AgentSkill,
}


class ExposurePreviewRequest(BaseModel):
    """A proposed change; only the fields it sets are read."""

    entity_type: Literal[
        "integration",
        "agent",
        "assignment",
        "flow",
        "knowledge_base",
        "skill",
        "general_assistant",
    ]
    entity_id: str | None = None
    integration_ids: list[str] | None = Field(default=None, max_length=500)
    graph: dict[str, Any] | None = None
    is_company_managed: bool | None = None
    access_restricted: bool | None = None
    shared_group_ids: list[str] | None = Field(default=None, max_length=500)
    shared_user_ids: list[str] | None = Field(default=None, max_length=2000)
    web_enabled: bool | None = None
    new_owner_id: str | None = None
    withhold_connected_account: bool | None = None


def _change(body: ExposurePreviewRequest) -> dict[str, Any]:
    change: dict[str, Any] = {}
    for name in body.model_fields_set - {"entity_type", "entity_id"}:
        value = getattr(body, name)
        if name in ("shared_group_ids", "shared_user_ids") and value is None:
            value = []
        change[name] = value
    return change


async def _load_manageable(
    db: AsyncSession, user: User, entity_type: str, entity_id: str | None
) -> tuple[Any, Any]:
    """The entity (and an assignment's agent) the caller may manage, else 404 / 403."""
    if entity_type == "general_assistant":
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Only admins configure the General Assistant.")
        config = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
        return config, None
    parsed = safe_uuid(entity_id) if entity_id else None
    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")
    groups = await get_user_group_ids(db, user.id)
    parent = None
    if entity_type == "assignment":
        assignment = await db.get(Assignment, parsed)
        parent = await db.get(Agent, assignment.agent_id) if assignment is not None else None
        visible = parent is not None and (
            await db.execute(
                select(Agent.id).where(
                    Agent.id == parent.id,
                    user_visible_filter(Agent, user.id, groups, is_admin=user.is_admin),
                )
            )
        ).scalar_one_or_none()
        if not visible:
            raise HTTPException(status_code=404, detail="Not found")
        if not can_manage(user, parent, groups):
            raise HTTPException(status_code=403, detail="You cannot configure this assignment.")
        return assignment, parent
    model = _ENTITY_MODELS[entity_type]
    entity = (
        await db.execute(
            select(model).where(
                model.id == parsed,
                user_visible_filter(model, user.id, groups, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not can_manage(user, entity, groups):
        raise HTTPException(status_code=403, detail="You cannot configure this item.")
    return entity, parent


@exposure_router.get("/exposure/summary")
async def get_exposure_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """How many ways each integration the caller manages is shared (list cards)."""
    groups = await get_user_group_ids(db, user.id)
    rows = (
        await db.execute(
            select(Integration).where(user_visible_filter(Integration, user.id, groups))
        )
    ).scalars().all()
    manageable = [row for row in rows if can_manage(user, row, groups)]
    summaries, truncated = await exposure_summaries(db, manageable)
    return {"summaries": summaries, "truncated": truncated}


@exposure_router.post("/exposure/preview")
async def post_exposure_preview(
    body: ExposurePreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Whose stored credentials the proposed change lets new people act with."""
    entity, parent = await _load_manageable(db, user, body.entity_type, body.entity_id)
    if entity is None:
        return {"credentials": [], "requires_confirmation": False}
    return await preview_exposure(
        db,
        actor_id=user.id,
        actor_is_admin=user.is_admin,
        entity_type=body.entity_type,
        entity=entity,
        change=_change(body),
        parent=parent,
    )


@exposure_router.get("/{integration_id}/exposure")
async def get_integration_exposure(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every way this integration's credential reaches people, and why."""
    parsed = parse_path_id(integration_id, not_found_detail="Integration not found")
    groups = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, str(parsed), groups, is_admin=user.is_admin
    )
    if not can_manage(user, integration, groups):
        raise HTTPException(status_code=403, detail=EXPOSURE_FORBIDDEN_DETAIL)
    return await integration_exposure(
        db, integration, viewer_id=user.id, viewer_is_admin=user.is_admin
    )
