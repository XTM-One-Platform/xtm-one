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

"""Custom tool sources API - sync custom tools from a GitHub or GitLab repository.

A source names a repository, a ref and a sub-directory; each directory with a
``tool.yaml`` manifest becomes one custom tool (``services/custom_tool_sources.py``).

Visibility: a personal source is seen and managed by its owner; a
company-managed source by admins only (its tools are company-managed, visible
to everyone as tools). Admins see every source. ``is_company_managed`` is set
at creation and never changes: the tools would otherwise change scope behind
the agents that use them.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import get_user_integration, parse_path_id, resolve_owner_names
from app.api.v1.schemas.column_limits import column_length_validator
from app.core.database import get_db
from app.core.security_options import security_option
from app.models.custom_tool import CustomTool
from app.models.custom_tool_source import CustomToolSource
from app.models.integration import Integration
from app.models.user import User
from app.services.audit import log_audit
from app.services.custom_tool_executor import CUSTOM_TOOLS_DISABLED_MSG
from app.services.custom_tool_sources import (
    SourceSyncBusyError,
    delete_source_tools,
    detach_source_tools,
    normalize_repository,
    normalize_subpath,
    run_sync,
    source_web_url,
)

router = APIRouter()

Provider = Literal["github", "gitlab"]
Interval = Literal["manual", "6h", "12h", "daily", "weekly"]
# The same vocabularies as the model's (pinned by tests/test_custom_tool_sources.py).


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CustomToolSourceCreate(BaseModel):
    name: str = Field(min_length=1)
    provider: Provider
    #: ``owner/repo`` / ``group/project``, or the repository's web URL.
    repository: str = Field(min_length=1)
    ref: str | None = None
    path: str = ""
    integration_id: str | None = None
    sync_interval: Interval = "manual"
    enabled: bool = True
    is_company_managed: bool = False

    _fit_columns = column_length_validator(CustomToolSource, "name", "ref", stripped=True)


class CustomToolSourceUpdate(BaseModel):
    name: str | None = None
    repository: str | None = None
    #: ``""`` tracks the default branch again.
    ref: str | None = None
    path: str | None = None
    #: ``""`` unbinds the integration (anonymous reads).
    integration_id: str | None = None
    sync_interval: Interval | None = None
    enabled: bool | None = None
    #: The detached tool directories; remove one to let the sync manage it again.
    detached_paths: list[str] | None = None

    _fit_columns = column_length_validator(CustomToolSource, "name", "ref", stripped=True)


class CustomToolSourceResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    provider: str
    repository: str
    repository_url: str
    ref: str | None = None
    path: str = ""
    integration_id: str | None = None
    integration_name: str | None = None
    sync_interval: str
    enabled: bool
    is_company_managed: bool
    detached_paths: list[str] = []
    last_synced_at: str | None = None
    last_commit_sha: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_sync_report: dict | None = None
    tool_count: int = 0
    can_manage: bool = False
    created_at: str
    updated_at: str


class CustomToolSourceSyncResponse(BaseModel):
    source: CustomToolSourceResponse
    #: ``None`` when the repository could not be read (``source.last_sync_error``).
    report: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_custom_tools_enabled() -> None:
    if security_option("disable_custom_tools"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CUSTOM_TOOLS_DISABLED_MSG)


def _can_manage(user: User, source: CustomToolSource) -> bool:
    if user.is_admin:
        return True
    return source.user_id == user.id and not source.is_company_managed


def _visible(user: User):
    if user.is_admin:
        return True
    return (CustomToolSource.user_id == user.id) & CustomToolSource.is_company_managed.is_(False)


async def _load(db: AsyncSession, user: User, source_id: str, *, manage: bool) -> CustomToolSource:
    sid = parse_path_id(source_id, not_found_detail="Source not found")
    source = (
        await db.execute(select(CustomToolSource).where(CustomToolSource.id == sid, _visible(user)))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if manage and not _can_manage(user, source):
        raise HTTPException(status_code=403, detail="You cannot manage this source.")
    return source


async def _bind_integration(
    db: AsyncSession, user: User, provider: str, integration_id: str
) -> uuid.UUID:
    """Validate the integration a source reads its repository with.

    The contract of skill and KB sync sources: one the caller can see, of the
    source's provider, connected, sharing its account (the sync runs
    unattended), and whose token the caller may use.
    """
    from app.services.integration_credentials import require_oauth_credentials_usable
    from app.services.integration_user_credentials import is_per_user

    integration = await get_user_integration(db, user.id, integration_id, is_admin=user.is_admin)
    if integration.provider != provider:
        raise HTTPException(
            status_code=422,
            detail=f"Integration '{integration.name}' is a {integration.provider} integration, not a {provider} one.",
        )
    if integration.status != "connected":
        raise HTTPException(
            status_code=422,
            detail=f"Integration '{integration.name}' is not connected (status: {integration.status}).",
        )
    if is_per_user(integration):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Integration '{integration.name}' acts with each person's own account, so it cannot read "
                "a source that syncs unattended. Bind an integration that shares its account."
            ),
        )
    require_oauth_credentials_usable(integration, user.id, is_admin=user.is_admin)
    return integration.id


def _normalized(provider: str, repository: str, path: str | None) -> tuple[str, str]:
    try:
        return normalize_repository(provider, repository), normalize_subpath(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _responses(
    db: AsyncSession, user: User, sources: list[CustomToolSource]
) -> list[CustomToolSourceResponse]:
    ids = [s.id for s in sources]
    counts: dict[uuid.UUID, int] = {}
    if ids:
        rows = await db.execute(
            select(CustomTool.source_id, func.count())
            .where(CustomTool.source_id.in_(ids))
            .group_by(CustomTool.source_id)
        )
        counts = {sid: n for sid, n in rows.all()}
    integration_ids = {s.integration_id for s in sources if s.integration_id}
    integration_names: dict[uuid.UUID, str] = {}
    if integration_ids:
        rows = await db.execute(
            select(Integration.id, Integration.name).where(Integration.id.in_(integration_ids))
        )
        integration_names = {iid: name for iid, name in rows.all()}
    owners = await resolve_owner_names(db, sources)
    return [
        CustomToolSourceResponse(
            id=str(s.id),
            user_id=str(s.user_id) if s.user_id else None,
            owner_name=owners.get(str(s.user_id)),
            name=s.name,
            provider=s.provider,
            repository=s.repository,
            repository_url=source_web_url(s),
            ref=s.ref,
            path=s.path or "",
            integration_id=str(s.integration_id) if s.integration_id else None,
            integration_name=integration_names.get(s.integration_id) if s.integration_id else None,
            sync_interval=s.sync_interval,
            enabled=s.enabled,
            is_company_managed=s.is_company_managed,
            detached_paths=list(s.detached_paths or []),
            last_synced_at=s.last_synced_at.isoformat() if s.last_synced_at else None,
            last_commit_sha=s.last_commit_sha,
            last_sync_status=s.last_sync_status,
            last_sync_error=s.last_sync_error,
            last_sync_report=s.last_sync_report,
            tool_count=counts.get(s.id, 0),
            can_manage=_can_manage(user, s),
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )
        for s in sources
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CustomToolSourceResponse])
async def list_sources(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The sources the caller sees: their own (admins: all)."""
    rows = await db.execute(
        select(CustomToolSource).where(_visible(user)).order_by(CustomToolSource.name)
    )
    return await _responses(db, user, list(rows.scalars()))


@router.post("", response_model=CustomToolSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: CustomToolSourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a source. Nothing is read from the repository until a sync."""
    _require_custom_tools_enabled()
    if body.is_company_managed and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can create company-managed sources")
    repository, path = _normalized(body.provider, body.repository, body.path)
    integration_id = (
        await _bind_integration(db, user, body.provider, body.integration_id) if body.integration_id else None
    )
    source = CustomToolSource(
        user_id=user.id,
        name=body.name.strip(),
        provider=body.provider,
        repository=repository,
        ref=(body.ref or "").strip() or None,
        path=path,
        integration_id=integration_id,
        sync_interval=body.sync_interval,
        enabled=body.enabled,
        is_company_managed=body.is_company_managed,
    )
    db.add(source)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="custom_tool_source",
        entity_id=source.id,
        entity_name=source.name,
        summary=f"Created custom tool source '{source.name}' ({source.provider} {source.repository})",
        request=request,
        details={"provider": source.provider, "repository": repository, "ref": source.ref, "path": path},
    )
    await db.commit()
    await db.refresh(source)
    return (await _responses(db, user, [source]))[0]


@router.get("/{source_id}", response_model=CustomToolSourceResponse)
async def get_source(
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source = await _load(db, user, source_id, manage=False)
    return (await _responses(db, user, [source]))[0]


@router.put("/{source_id}", response_model=CustomToolSourceResponse)
async def update_source(
    source_id: str,
    body: CustomToolSourceUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a source. A changed repository, ref or path applies at the next sync."""
    source = await _load(db, user, source_id, manage=True)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="A source needs a name.")
        source.name = body.name.strip()
    if body.repository is not None or body.path is not None:
        source.repository, source.path = _normalized(
            source.provider,
            body.repository if body.repository is not None else source.repository,
            body.path if body.path is not None else source.path,
        )
    if body.ref is not None:
        source.ref = body.ref.strip() or None
    if body.integration_id is not None:
        new_id = body.integration_id.strip()
        if not new_id:
            source.integration_id = None
        elif new_id != (str(source.integration_id) if source.integration_id else None):
            source.integration_id = await _bind_integration(db, user, source.provider, new_id)
    if body.sync_interval is not None:
        source.sync_interval = body.sync_interval
    if body.enabled is not None:
        source.enabled = body.enabled
    if body.detached_paths is not None:
        # Only paths already detached can stay: a path is detached from a tool.
        current = set(source.detached_paths or [])
        source.detached_paths = sorted(p for p in body.detached_paths if p in current) or None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_tool_source",
        entity_id=source.id,
        entity_name=source.name,
        summary=f"Updated custom tool source '{source.name}'",
        request=request,
        details={"repository": source.repository, "ref": source.ref, "path": source.path},
    )
    await db.commit()
    await db.refresh(source)
    return (await _responses(db, user, [source]))[0]


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: str,
    request: Request,
    delete_tools: bool = Query(
        False, description="Delete the source's tools too; by default they are detached and kept."
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a source. Its tools are detached (kept, editable) unless *delete_tools*."""
    source = await _load(db, user, source_id, manage=True)
    if delete_tools:
        count = await delete_source_tools(db, source.id)
    else:
        count = await detach_source_tools(db, source.id)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="custom_tool_source",
        entity_id=source.id,
        entity_name=source.name,
        summary=(
            f"Deleted custom tool source '{source.name}' and "
            f"{'deleted' if delete_tools else 'detached'} its {count} tool(s)"
        ),
        request=request,
        details={"delete_tools": delete_tools, "tools": count},
    )
    await db.delete(source)
    await db.commit()


@router.post("/{source_id}/sync", response_model=CustomToolSourceSyncResponse)
async def sync_source_now(
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Sync the source now: read the repository, run the tests of every new or
    changed tool, and activate the versions that pass."""
    _require_custom_tools_enabled()
    source = await _load(db, user, source_id, manage=True)
    try:
        report = await run_sync(db, source, actor=user)
    except SourceSyncBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await db.refresh(source)
    return CustomToolSourceSyncResponse(
        source=(await _responses(db, user, [source]))[0],
        report=report.as_dict() if report else None,
    )
