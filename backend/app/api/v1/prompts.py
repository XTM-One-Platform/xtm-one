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

"""Prompt management API – CRUD for reusable prompt templates.

Any authenticated user can create prompts. Company-managed prompts
(``is_company_managed=True``) are visible to all users but only admins can
edit/delete them; visibility comes from the flag and ``user_id`` keeps the
creator as owner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_ai_enabled
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    get_user_prompt,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.prompt import (
    ImportPromptRequest,
    PromptCreate,
    PromptResponse,
    PromptUpdate,
)
from app.api.v1.schemas.versioning import (
    RollbackRequest,
    VersionDetailResponse,
    VersionDiffResponse,
    VersionListItemResponse,
    VersionListResponse,
)
from app.api.v1.versioning_helpers import to_version_detail, to_version_item
from app.core.database import get_db
from app.models.agent import Agent
from app.models.prompt import Prompt
from app.models.user import User
from app.services.audit import log_audit
from app.services.prompt_versioning import (
    build_version_diff,
    create_prompt_version,
    get_active_prompt_version_number,
    get_prompt_version,
    is_prompt_version_pinned_by_evaluation_runs,
    list_prompt_versions,
    resolve_author_names,
    rollback_prompt_to_version,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(
    prompt: Prompt, *, can_manage: bool, owner_name: str | None = None
) -> PromptResponse:
    return PromptResponse(
        id=str(prompt.id),
        user_id=str(prompt.user_id) if prompt.user_id else None,
        owner_name=owner_name,
        name=prompt.name,
        description=prompt.description,
        content=prompt.content,
        tags=prompt.tags or [],
        hide_from_chat=prompt.hide_from_chat,
        is_company_managed=prompt.is_company_managed,
        is_public_catalog=prompt.is_public_catalog,
        shared_group_ids=prompt.shared_group_ids,
        shared_group_permissions=prompt.shared_group_permissions,
        shared_user_ids=prompt.shared_user_ids,
        shared_user_permissions=prompt.shared_user_permissions,
        access_restricted=prompt.access_restricted,
        can_manage=can_manage,
        created_at=prompt.created_at.isoformat(),
        updated_at=prompt.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PromptResponse])
async def list_prompts(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List prompts visible to the current user (own + group-shared + company-managed).

    ``scope=all`` lists every prompt for an admin (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            Prompt, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    prompts = result.scalars().all()
    names = await resolve_owner_names(db, prompts)
    return [
        _to_response(
            p,
            can_manage=can_manage(user, p, user_group_ids),
            owner_name=names.get(str(p.user_id)),
        )
        for p in prompts
    ]


@router.post("", response_model=PromptResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PromptCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new prompt."""
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed prompts"
    )

    prompt = Prompt(
        user_id=user.id,
        name=body.name,
        description=body.description,
        content=body.content,
        tags=body.tags or [],
        hide_from_chat=body.hide_from_chat,
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, prompt, body, partial=False)
    await apply_user_share_fields(db, prompt, body, partial=False)
    db.add(prompt)
    await db.flush()
    await create_prompt_version(
        db,
        prompt_id=prompt.id,
        content=prompt.content,
        author_id=user.id,
        commit_message="Initial version",
    )
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="prompt",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Created prompt '{prompt.name}'",
        details={
            "content_preview": (prompt.content or "")[:200],
            "is_company_managed": prompt.is_company_managed,
            "tags": prompt.tags or [],
            "hide_from_chat": prompt.hide_from_chat,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(prompt)
    return _to_response(
        prompt, can_manage=can_manage(user, prompt), owner_name=user.full_name
    )


@router.get("/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single prompt (must be own, group-shared, company-managed, or admin)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    owner_name = None
    if prompt.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == prompt.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        prompt,
        can_manage=can_manage(user, prompt, user_group_ids),
        owner_name=owner_name,
    )


@router.put("/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: str,
    body: PromptUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a prompt (creator or admin only)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_manageable_entity(
        db,
        Prompt,
        user,
        prompt_id,
        action="edit",
        entity_label="Prompt",
        user_group_ids=user_group_ids,
    )

    if body.name is not None:
        prompt.name = body.name
    if body.description is not None:
        prompt.description = body.description
    content_changed = body.content is not None and body.content != prompt.content
    if body.content is not None:
        prompt.content = body.content
    if body.tags is not None:
        prompt.tags = body.tags
    if body.hide_from_chat is not None:
        prompt.hide_from_chat = body.hide_from_chat
    # Group sharing
    await apply_group_share_fields(db, prompt, body, partial=True)

    await handle_company_managed_toggle(
        db,
        user,
        prompt,
        body.is_company_managed,
        agent_field="prompt_ids",
        keep_grants=wants_access_restriction(body, prompt),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, prompt, body, partial=True)

    # Owner transfer (admin only)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=prompt,
        new_owner_id=body.user_id,
        entity_type="prompt",
        entity_name=prompt.name,
        request=request,
    )

    # Public catalog toggle (admin-only for company-managed entities)
    if body.is_public_catalog is not None:
        if prompt.is_company_managed and not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can publish company-managed entities to the public catalog",
            )
        prompt.is_public_catalog = body.is_public_catalog

    created_version = None
    if content_changed:
        created_version = await create_prompt_version(
            db,
            prompt_id=prompt.id,
            content=prompt.content,
            author_id=user.id,
            commit_message=body.commit_message,
        )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="prompt",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Updated prompt '{prompt.name}'",
        details={
            "is_company_managed": prompt.is_company_managed,
            "tags": prompt.tags or [],
            "hide_from_chat": prompt.hide_from_chat,
            "new_version": created_version.version_number if created_version else None,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(prompt)
    owner_name = None
    if prompt.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == prompt.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        prompt,
        can_manage=can_manage(user, prompt, user_group_ids),
        owner_name=owner_name,
    )


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a prompt (creator or admin only)."""
    prompt = await get_manageable_entity(
        db, Prompt, user, prompt_id, action="delete", entity_label="Prompt"
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="prompt",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Deleted prompt '{prompt.name}'",
        details={
            "is_company_managed": prompt.is_company_managed,
        },
        request=request,
    )
    # Purge stale references from all agents' prompt_ids. Pre-filter with a
    # text ``contains`` so only rows that actually mention the id are loaded
    # instead of every agent in the deployment (the exact check stays
    # authoritative).
    pid = str(prompt.id)
    agents_result = await db.execute(
        select(Agent).where(cast(Agent.prompt_ids, Text).contains(pid))
    )
    for agent in agents_result.scalars().all():
        if agent.prompt_ids and pid in agent.prompt_ids:
            agent.prompt_ids = [v for v in agent.prompt_ids if v != pid]

    await db.delete(prompt)
    await db.commit()


@router.get("/{prompt_id}/versions", response_model=VersionListResponse)
async def get_prompt_versions(
    prompt_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List prompt versions ordered by latest first."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    versions, total = await list_prompt_versions(
        db,
        prompt_id=prompt.id,
        limit=limit,
        offset=offset,
    )
    active_version_number = await get_active_prompt_version_number(db, prompt_id=prompt.id)
    names = await resolve_author_names(db, versions)
    items = [
        to_version_item(
            v,
            author_name=names.get(str(v.author_id)) if v.author_id else None,
            is_active=active_version_number is not None and v.version_number == active_version_number,
        )
        for v in versions
    ]
    return VersionListResponse(items=items, total=total)


@router.get("/{prompt_id}/versions/diff", response_model=VersionDiffResponse)
async def diff_prompt_versions(
    prompt_id: str,
    from_version: int = Query(..., alias="from", ge=1),
    to_version: int = Query(..., alias="to", ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compute line-by-line diff between two prompt versions."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    left = await get_prompt_version(
        db, prompt_id=prompt.id, version_number=from_version
    )
    right = await get_prompt_version(db, prompt_id=prompt.id, version_number=to_version)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Version not found")

    return VersionDiffResponse(
        **build_version_diff(
            from_content=left.content,
            to_content=right.content,
            from_version=from_version,
            to_version=to_version,
        )
    )


@router.get("/{prompt_id}/versions/{version_number}", response_model=VersionDetailResponse)
async def get_prompt_version_detail(
    prompt_id: str,
    version_number: Annotated[int, Path(ge=1)],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific prompt version by number."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    version = await get_prompt_version(
        db,
        prompt_id=prompt.id,
        version_number=version_number,
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_prompt_version_number(db, prompt_id=prompt.id)
    return to_version_detail(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=(
            active_version_number is not None
            and version.version_number == active_version_number
        ),
    )


@router.post(
    "/{prompt_id}/versions/{version_number}/rollback",
    response_model=VersionDetailResponse,
)
async def rollback_prompt_version(
    prompt_id: str,
    version_number: Annotated[int, Path(ge=1)],
    body: RollbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Rollback prompt content to a previous version as a new head version."""
    prompt = await get_manageable_entity(
        db, Prompt, user, prompt_id, action="edit", entity_label="Prompt"
    )
    target = await get_prompt_version(
        db,
        prompt_id=prompt.id,
        version_number=version_number,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Version not found")

    active_version = await rollback_prompt_to_version(
        db,
        prompt=prompt,
        target_version=target,
        author_id=user.id,
        commit_message=body.commit_message,
    )
    await log_audit(
        db,
        user=user,
        action="rollback",
        entity_type="prompt",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Rolled back prompt '{prompt.name}' to v{version_number}",
        details={
            "rolled_back_to": version_number,
            "active_version": active_version.version_number,
            "rollback_note": body.commit_message,
        },
        request=request,
    )
    await db.commit()

    names = await resolve_author_names(db, [active_version])
    return to_version_detail(
        active_version,
        author_name=(
            names.get(str(active_version.author_id))
            if active_version.author_id
            else None
        ),
        # ``rollback_prompt_to_version`` creates a new head version with
        # the target's content, so the freshly-returned ``active_version``
        # IS the active head by construction.  Pass ``is_active=True``
        # explicitly so the response contract matches the list endpoint
        # (which would return ``is_active=true`` for the same row).
        is_active=True,
    )


@router.delete("/{prompt_id}/versions/{version_number}", status_code=204)
async def delete_prompt_version(
    prompt_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a prompt version. Admin only. Cannot delete the active version or a locked version."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    version = await get_prompt_version(db, prompt_id=prompt.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    active_version_number = await get_active_prompt_version_number(db, prompt_id=prompt.id)
    if active_version_number is not None and version.version_number == active_version_number:
        raise HTTPException(status_code=409, detail="Cannot delete the active version")
    if version.is_locked:
        raise HTTPException(status_code=409, detail="Version is locked; unlock it first")
    if await is_prompt_version_pinned_by_evaluation_runs(
        db,
        prompt_id=prompt.id,
        version_number=version.version_number,
    ):
        raise HTTPException(
            status_code=409,
            detail="Version is referenced by a pending/running evaluation run",
        )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="prompt_version",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Deleted version v{version_number} of prompt '{prompt.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.delete(version)
    await db.commit()


@router.post("/{prompt_id}/versions/{version_number}/lock", response_model=VersionListItemResponse)
async def lock_prompt_version(
    prompt_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Lock a prompt version to protect it from deletion. Admin only."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    version = await get_prompt_version(db, prompt_id=prompt.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version.is_locked = True
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="prompt_version",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Locked version v{version_number} of prompt '{prompt.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.commit()
    await db.refresh(version)
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_prompt_version_number(db, prompt_id=prompt.id)
    return to_version_item(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=active_version_number is not None and version.version_number == active_version_number,
    )


@router.post("/{prompt_id}/versions/{version_number}/unlock", response_model=VersionListItemResponse)
async def unlock_prompt_version(
    prompt_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Unlock a prompt version. Admin only."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )
    version = await get_prompt_version(db, prompt_id=prompt.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version.is_locked = False
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="prompt_version",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Unlocked version v{version_number} of prompt '{prompt.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.commit()
    await db.refresh(version)
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_prompt_version_number(db, prompt_id=prompt.id)
    return to_version_item(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=active_version_number is not None and version.version_number == active_version_number,
    )


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/{prompt_id}/export")
async def export_prompt(
    prompt_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a prompt as a portable JSON bundle."""
    user_group_ids = await get_user_group_ids(db, user.id)
    prompt = await get_user_prompt(
        db, user.id, prompt_id, user_group_ids, is_admin=user.is_admin
    )

    bundle = {
        "version": 1,
        "export_type": "prompt",
        "exported_at": datetime.now(UTC).isoformat(),
        "prompt": {
            "name": prompt.name,
            "description": prompt.description,
            "content": prompt.content,
            "tags": prompt.tags or [],
        },
    }
    from app.core.utils import build_content_disposition

    safe_name = prompt.name.lower().replace(" ", "-")
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": build_content_disposition(
                f"prompt-{safe_name}.json"
            )
        },
    )


@router.post(
    "/import", response_model=PromptResponse, status_code=status.HTTP_201_CREATED
)
async def import_prompt(
    body: ImportPromptRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import a prompt from an export bundle."""
    if body.version != 1:
        raise HTTPException(
            status_code=400, detail=f"Unsupported export version: {body.version}"
        )
    if body.export_type != "prompt":
        raise HTTPException(
            status_code=400, detail=f"Unsupported export type: {body.export_type}"
        )

    data = body.prompt
    if not data.get("name"):
        raise HTTPException(status_code=400, detail="Prompt name is required")

    prompt = Prompt(
        user_id=user.id,
        name=data["name"],
        description=data.get("description", ""),
        content=data.get("content", ""),
        tags=data.get("tags", []),
        is_company_managed=False,
    )
    db.add(prompt)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="prompt",
        entity_id=prompt.id,
        entity_name=prompt.name,
        summary=f"Imported prompt '{prompt.name}'",
        details={
            "tags": prompt.tags or [],
        },
        request=request,
    )
    await db.commit()
    await db.refresh(prompt)
    return _to_response(
        prompt, can_manage=can_manage(user, prompt), owner_name=user.full_name
    )
