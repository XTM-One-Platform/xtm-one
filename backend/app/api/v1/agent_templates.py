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

"""Agent template endpoints – CRUD for DB-backed agent templates.

Templates are admin-managed. Built-in templates are seeded at startup and are
fully editable afterward. Users can only list enabled templates.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_ai_enabled
from app.api.v1.helpers import get_user_group_ids
from app.api.v1.schemas.agent import (
    AgentTemplateResponse,
    CreateTemplateRequest,
    UpdateTemplateRequest,
)
from app.core.database import get_db
from app.models.agent_template import AgentTemplate
from app.models.user import User
from app.services.agent_template_skills import (
    ordered_skill_ids,
    template_skill_slugs,
    visible_skill_ids_by_slug,
)
from app.services.audit import log_audit

template_router = APIRouter()


def _to_template_response(
    t: AgentTemplate, skill_ids_by_slug: dict[str, str] | None = None
) -> AgentTemplateResponse:
    """The one serializer for every template endpoint.

    Extracted because the three hand-built constructions this replaces had
    already drifted: none of them carried ``required_integration_types``, so
    the advanced creation form's integration-binding step silently never
    rendered for templates that declared required types.

    *skill_ids_by_slug* is the caller's resolution of the default skills
    (:func:`_default_skill_resolution`); without it ``default_skill_ids`` is
    empty.
    """
    return AgentTemplateResponse(
        id=str(t.id),
        slug=t.slug,
        name=t.name,
        description=t.description,
        icon=t.icon,
        category=t.category,
        disable_chat=t.disable_chat,
        default_output_format=t.default_output_format,
        required_integrations=t.required_integrations,
        required_integration_types=t.required_integration_types,
        recommended_mcp=t.recommended_mcp,
        must_be_defined=t.must_be_defined,
        default_persona=t.default_persona,
        default_skills=template_skill_slugs(t.default_skills),
        default_skill_ids=ordered_skill_ids(t.default_skills, skill_ids_by_slug or {}),
        assignment_count=len(t.default_assignments or []),
        is_builtin=t.is_builtin,
        enabled=t.enabled,
    )


# ---------------------------------------------------------------------------
# Templates (DB-backed, admin-managed)
# ---------------------------------------------------------------------------


async def _default_skill_resolution(
    db: AsyncSession, user: User, templates: list[AgentTemplate]
) -> dict[str, str]:
    """``slug -> skill id`` for every default skill of *templates* the caller may attach."""
    slugs = {s for t in templates for s in template_skill_slugs(t.default_skills)}
    if not slugs:
        return {}
    return await visible_skill_ids_by_slug(
        db,
        slugs,
        user_id=user.id,
        user_group_ids=await get_user_group_ids(db, user.id),
        is_admin=user.is_admin,
    )


@template_router.get("/templates", response_model=list[AgentTemplateResponse])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List available agent templates (enabled ones for users, all for admins)."""
    result = await db.execute(
        select(AgentTemplate)
        .where(AgentTemplate.enabled.is_(True))
        .order_by(AgentTemplate.name)
    )
    templates = list(result.scalars().all())
    by_slug = await _default_skill_resolution(db, user, templates)
    return [_to_template_response(t, by_slug) for t in templates]


@template_router.post(
    "/templates", response_model=AgentTemplateResponse, status_code=201
)
async def create_template(
    body: CreateTemplateRequest,
    request: Request,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a new agent template (admin only)."""
    existing = await db.execute(
        select(AgentTemplate).where(AgentTemplate.slug == body.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Template with slug '{body.slug}' already exists"
        )

    template = AgentTemplate(
        slug=body.slug,
        name=body.name,
        description=body.description,
        icon=body.icon,
        category=body.category,
        disable_chat=body.disable_chat,
        default_output_format=body.default_output_format,
        default_persona=body.default_persona,
        required_integrations=body.required_integrations or [],
        required_integration_types=body.required_integration_types or [],
        recommended_mcp=body.recommended_mcp or [],
        must_be_defined=[r.model_dump() for r in body.must_be_defined or []],
        default_skills=template_skill_slugs(body.default_skills),
        default_assignments=body.default_assignments or [],
        is_builtin=False,
        enabled=True,
    )
    db.add(template)
    await db.flush()
    await log_audit(
        db,
        user=_admin,
        action="create",
        entity_type="template",
        entity_id=template.id,
        entity_name=template.name,
        summary=f"Created agent template '{template.name}' ({template.slug})",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return _to_template_response(
        template, await _default_skill_resolution(db, _admin, [template])
    )


@template_router.put("/templates/{slug}", response_model=AgentTemplateResponse)
async def update_template(
    slug: str,
    body: UpdateTemplateRequest,
    request: Request,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update an agent template (admin only). Works for both built-in and custom."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.slug == slug))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    for field in [
        "name",
        "description",
        "icon",
        "category",
        "disable_chat",
        "default_output_format",
        "default_persona",
        "required_integrations",
        "required_integration_types",
        "recommended_mcp",
        "must_be_defined",
        "default_skills",
        "default_assignments",
        "enabled",
    ]:
        value = getattr(body, field, None)
        if value is None:
            continue
        if field == "must_be_defined":
            value = [r.model_dump() for r in value]
        elif field == "default_skills":
            value = template_skill_slugs(value)
        setattr(template, field, value)

    await log_audit(
        db,
        user=_admin,
        action="update",
        entity_type="template",
        entity_id=template.id,
        entity_name=template.name,
        summary=f"Updated agent template '{template.name}' ({slug})",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return _to_template_response(
        template, await _default_skill_resolution(db, _admin, [template])
    )


@template_router.delete("/templates/{slug}", status_code=204)
async def delete_template(
    slug: str,
    request: Request,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete an agent template (admin only)."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.slug == slug))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    await log_audit(
        db,
        user=_admin,
        action="delete",
        entity_type="template",
        entity_id=template.id,
        entity_name=template.name,
        summary=f"Deleted agent template '{template.name}' ({slug})",
        request=request,
    )
    await db.delete(template)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
