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

"""Who can use a resource a person lends to others, and a preview of what a
change would lend or take back. Read-only, additive routes.

* ``GET /exposure/{resources}/{id}`` - every path (direct shares, agents,
  assignments, flows, knowledge bases, skills, prompts, custom tools, the
  General Assistant) with its reason and audience, for someone who can manage
  the resource (an admin reaching it by navigation included, decision 9).
* ``GET /exposure/{resources}/summary`` - the list cards' counts, over the
  resources the caller sees AND manages (three-tier, never an admin-wide list).
* ``POST /exposure/preview`` - "if I save this, whose resources become usable by
  whom, and who loses them", for the confirmation shown before a share, a
  binding, a creation, an unshare or a deletion, and, for administrators, a
  change of a group's members or of the agents the platform MCP key reaches.
  It also lists the knowledge bases, skills and prompts a change lends through
  what attaches them (``resource_exposure.ATTACHED_ONLY_KINDS``).

``{resources}`` is ``integrations``, ``mcp-servers``, ``custom-tools``,
``channel-bots`` or ``variables``. The routes live under ``/exposure`` so they
never meet a resource router's own ``/{id}/...`` routes; the integration pages
keep the integration-only routes of ``integration_exposure``, which
agree with these on every path (pinned by ``test_resource_exposure_postgres``).
Nothing here writes or refuses a change.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    can_manage,
    get_user_channel_bot,
    get_user_custom_tool,
    get_user_group_ids,
    get_user_integration,
    get_user_mcp_server,
    parse_path_id,
    user_visible_filter,
)
from app.api.v1.schemas.exposure import (
    ExposurePreviewOut,
    ExposurePreviewRequest,
    ExposureSummariesOut,
    ResourceExposureOut,
)
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.flow import Flow
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.group import Group
from app.models.knowledge_base import KnowledgeBase
from app.models.platform_settings import PlatformSettings
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services import resource_exposure as exposure

router = APIRouter()

EXPOSURE_FORBIDDEN_DETAIL = "Only people who can manage this {label} can see who can use it."

#: URL segment -> (resource kind, label for messages).
_RESOURCE_ROUTES: dict[str, tuple[str, str]] = {
    "integrations": (exposure.RESOURCE_INTEGRATION, "integration"),
    "mcp-servers": (exposure.RESOURCE_MCP_SERVER, "MCP server"),
    "custom-tools": (exposure.RESOURCE_CUSTOM_TOOL, "custom tool"),
    "channel-bots": (exposure.RESOURCE_CHANNEL_BOT, "channel bot"),
    "variables": (exposure.RESOURCE_VARIABLE, "variable"),
}

_CARRIER_MODELS: dict[str, Any] = {
    exposure.ENTITY_AGENT: Agent,
    exposure.ENTITY_FLOW: Flow,
    exposure.ENTITY_KNOWLEDGE_BASE: KnowledgeBase,
    exposure.ENTITY_SKILL: AgentSkill,
    exposure.ENTITY_PROMPT: Prompt,
}


async def _load_resource(db: AsyncSession, user: User, kind: str, resource_id: str) -> Any:
    """The resource as its own detail route loads it (404 when not visible)."""
    groups = await get_user_group_ids(db, user.id)
    if kind == exposure.RESOURCE_INTEGRATION:
        return await get_user_integration(db, user.id, resource_id, groups, is_admin=user.is_admin)
    if kind == exposure.RESOURCE_MCP_SERVER:
        return await get_user_mcp_server(db, user.id, resource_id, groups, is_admin=user.is_admin)
    if kind == exposure.RESOURCE_CUSTOM_TOOL:
        return await get_user_custom_tool(db, user.id, resource_id, groups, is_admin=user.is_admin)
    if kind == exposure.RESOURCE_CHANNEL_BOT:
        return await get_user_channel_bot(db, user.id, resource_id, groups, is_admin=user.is_admin)
    # Variables have no admin bypass on their own detail route either.
    parsed = parse_path_id(resource_id, not_found_detail="Variable not found")
    row = (
        await db.execute(select(Variable).where(Variable.id == parsed, user_visible_filter(Variable, user.id, groups)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    return row


def _make_routes(segment: str, kind: str, label: str) -> None:
    async def get_summary(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        groups = await get_user_group_ids(db, user.id)
        model = exposure.RESOURCE_MODELS[kind]
        rows = (await db.execute(select(model).where(user_visible_filter(model, user.id, groups)))).scalars().all()
        manageable = [row for row in rows if can_manage(user, row, groups)]
        summaries, truncated = await exposure.exposure_summaries(db, kind, manageable)
        return {"summaries": summaries, "truncated": truncated}

    async def get_exposure(
        resource_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        resource = await _load_resource(db, user, kind, resource_id)
        groups = await get_user_group_ids(db, user.id)
        if not can_manage(user, resource, groups):
            raise HTTPException(status_code=403, detail=EXPOSURE_FORBIDDEN_DETAIL.format(label=label))
        return await exposure.resource_exposure(db, kind, resource, viewer_id=user.id, viewer_is_admin=user.is_admin)

    router.add_api_route(
        f"/exposure/{segment}/summary",
        get_summary,
        methods=["GET"],
        response_model=ExposureSummariesOut,
        name=f"{kind}_exposure_summary",
        summary=f"How many ways each {label} you manage is shared",
    )
    router.add_api_route(
        f"/exposure/{segment}/{{resource_id}}",
        get_exposure,
        methods=["GET"],
        response_model=ResourceExposureOut,
        name=f"{kind}_exposure",
        summary=f"Who can use this {label}, and through what",
    )


for _segment, (_kind, _label) in _RESOURCE_ROUTES.items():
    _make_routes(_segment, _kind, _label)


async def _load_for_preview(db: AsyncSession, user: User, body: ExposurePreviewRequest) -> tuple[Any, Any]:
    """(entity, parent) the caller may change: visible (an admin's navigation
    bypass included) and manageable, else 404 / 403. ``entity`` is ``None``
    when creating one."""
    entity_type = body.entity_type
    groups = await get_user_group_ids(db, user.id)
    if entity_type == exposure.ENTITY_GENERAL_ASSISTANT:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Only admins configure the General Assistant.")
        config = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
        return config, None
    if entity_type == exposure.ENTITY_PLATFORM_MCP:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Only admins configure the platform MCP endpoint.")
        return (await db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none(), None
    if entity_type == exposure.ENTITY_GROUP:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Only admins manage groups.")
        group = await db.get(Group, parse_path_id(body.entity_id or "", not_found_detail="Group not found"))
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found")
        return group, None

    if entity_type == exposure.ENTITY_ASSIGNMENT:
        if body.entity_id:
            parsed = safe_uuid(body.entity_id)
            assignment = await db.get(Assignment, parsed) if parsed else None
            if assignment is None:
                raise HTTPException(status_code=404, detail="Assignment not found")
            agent_id = assignment.agent_id
        else:
            assignment = None
            agent_id = safe_uuid(body.parent_id) if body.parent_id else None
        agent = (
            (
                await db.execute(
                    select(Agent).where(
                        Agent.id == agent_id,
                        user_visible_filter(Agent, user.id, groups, is_admin=user.is_admin),
                    )
                )
            ).scalar_one_or_none()
            if agent_id
            else None
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not can_manage(user, agent, groups):
            raise HTTPException(status_code=403, detail="You cannot configure this agent.")
        return assignment, agent

    if not body.entity_id:
        return None, None
    if entity_type in exposure.RESOURCE_KINDS and entity_type not in _CARRIER_MODELS:
        resource = await _load_resource(db, user, entity_type, body.entity_id)
        if not can_manage(user, resource, groups):
            raise HTTPException(status_code=403, detail="You cannot configure this item.")
        return resource, None
    model = _CARRIER_MODELS[entity_type]
    parsed = safe_uuid(body.entity_id)
    entity = (
        (
            await db.execute(
                select(model).where(
                    model.id == parsed,
                    user_visible_filter(model, user.id, groups, is_admin=user.is_admin),
                )
            )
        ).scalar_one_or_none()
        if parsed
        else None
    )
    if entity is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not can_manage(user, entity, groups):
        raise HTTPException(status_code=403, detail="You cannot configure this item.")
    return entity, None


@router.post(
    "/exposure/preview",
    response_model=ExposurePreviewOut,
    summary="Whose resources a proposed change lends to new people, or takes back",
)
async def post_exposure_preview(
    body: ExposurePreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    entity, parent = await _load_for_preview(db, user, body)
    return await exposure.preview_change(
        db,
        actor_id=user.id,
        actor_is_admin=user.is_admin,
        entity_type=body.entity_type,
        entity=entity,
        change=dict(body.change),
        parent=parent,
    )
