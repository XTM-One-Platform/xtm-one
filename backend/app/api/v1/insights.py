# SPDX-License-Identifier: Apache-2.0

"""Department-grade LLM analytics: the department mapping and the analytics export.

* ``/departments`` (admin): the departments, the ordered group rules and the
  resolution settings, edited as one mapping (``services/departments.py``),
  with a preview of who it resolves where; the directory departments
  imported from an HR export; and "apply the mapping to history", which
  resolves the department of every stored usage again
  (``tracing/attribution_backfill.restart_walk``).
* ``/analytics/export`` (any user, scoped like ``query_platform_analytics``:
  an administrator reads the platform, a group manager their teams, anyone
  else their own usage): the CSV of a read, every metric as a column, for a
  spreadsheet or a warehouse load.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.models.department import Department, DepartmentGroupRule, TraceAttributionBackfill
from app.models.group import Group
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.audit import log_audit
from app.services.departments import (
    DEPARTMENT_SOURCES,
    department_preview,
    invalidate_department_map,
    load_department_settings,
    normalize_settings,
)

router = APIRouter()

MAX_DEPARTMENTS = 500
MAX_DIRECTORY_ENTRIES = 50_000


class DepartmentItem(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    directory_values: list[str] = Field(default_factory=list)


class DepartmentRuleItem(BaseModel):
    group_id: str
    #: The department's name: a department of the same request may be new.
    department: str
    group_name: str | None = None
    group_kind: str | None = None


class DepartmentSettingsItem(BaseModel):
    sources: list[str] = Field(default_factory=lambda: list(DEPARTMENT_SOURCES))
    directory_attribute: str = "department"


class DepartmentMapping(BaseModel):
    departments: list[DepartmentItem] = Field(default_factory=list)
    #: In priority order: a person's first rule whose group they are in decides.
    rules: list[DepartmentRuleItem] = Field(default_factory=list)
    settings: DepartmentSettingsItem = Field(default_factory=DepartmentSettingsItem)


class DepartmentMappingResponse(DepartmentMapping):
    backfill: dict[str, Any] = Field(default_factory=dict)


class DirectoryEntry(BaseModel):
    email: str
    department: str | None = None


class DirectoryImport(BaseModel):
    entries: list[DirectoryEntry]


class HistoryRewrite(BaseModel):
    #: Resolve the department of every stored usage again from the current mapping.
    rewrite: bool = True


async def _backfill_state(db: AsyncSession) -> dict[str, Any]:
    state = await db.get(TraceAttributionBackfill, 1)
    if state is None:
        return {"started": False, "completed_at": None, "cursor_date": None, "rewrite_requested_at": None}
    return {
        "started": True,
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
        "cursor_date": state.cursor_date.isoformat() if state.cursor_date else None,
        "rewrite_requested_at": (
            state.department_rewrite_before.isoformat() if state.department_rewrite_before else None
        ),
    }


async def _mapping(db: AsyncSession) -> DepartmentMappingResponse:
    departments = (await db.execute(select(Department).order_by(func.lower(Department.name)))).scalars().all()
    names = {department.id: department.name for department in departments}
    rules = (
        await db.execute(
            select(DepartmentGroupRule, Group.name, Group.kind)
            .join(Group, Group.id == DepartmentGroupRule.group_id)
            .order_by(DepartmentGroupRule.priority.asc(), Group.name.asc())
        )
    ).all()
    settings = await load_department_settings(db)
    return DepartmentMappingResponse(
        departments=[
            DepartmentItem(
                id=str(department.id),
                name=department.name,
                description=department.description,
                directory_values=list(department.directory_values or []),
            )
            for department in departments
        ],
        rules=[
            DepartmentRuleItem(
                group_id=str(rule.group_id),
                department=names.get(rule.department_id, ""),
                group_name=group_name,
                group_kind=group_kind,
            )
            for rule, group_name, group_kind in rules
        ],
        settings=DepartmentSettingsItem(**settings.payload()),
        backfill=await _backfill_state(db),
    )


@router.get("/departments", response_model=DepartmentMappingResponse)
async def get_departments(_admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """The department mapping (admin-only)."""
    return await _mapping(db)


@router.put("/departments", response_model=DepartmentMappingResponse)
async def put_departments(
    body: DepartmentMapping,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Replace the department mapping (admin-only).

    A department left out is deleted: its stored usage keeps its id and reads
    as a deleted department. The history is resolved from the mapping once,
    when the first department is created; a later change applies to new usage
    only, unless an administrator applies it to history (``/departments/history``).
    """
    from app.services.tracing.attribution_backfill import restart_walk

    if len(body.departments) > MAX_DEPARTMENTS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_DEPARTMENTS} departments.")
    folded = [item.name.strip().casefold() for item in body.departments]
    if len(set(folded)) != len(folded) or any(not name for name in folded):
        raise HTTPException(status_code=422, detail="Department names must be non-empty and unique (any case).")
    unknown_sources = [source for source in body.settings.sources if source not in DEPARTMENT_SOURCES]
    if unknown_sources:
        raise HTTPException(status_code=422, detail=f"Sources must be among {', '.join(DEPARTMENT_SOURCES)}.")

    existing = {department.id: department for department in (await db.execute(select(Department))).scalars().all()}
    had_departments = bool(existing)
    kept: dict[str, Department] = {}
    for item in body.departments:
        parsed = safe_uuid(item.id) if item.id else None
        department = existing.get(parsed) if parsed else None
        values = sorted({value.strip()[:200] for value in item.directory_values if value and value.strip()}, key=str.casefold)
        if department is None:
            department = Department(id=uuid.uuid4())
            db.add(department)
        department.name = item.name.strip()
        department.description = (item.description or "").strip() or None
        department.directory_values = values or None
        kept[department.name.casefold()] = department
    kept_ids = {department.id for department in kept.values()}
    for department_id, department in existing.items():
        if department_id not in kept_ids:
            await db.delete(department)
    await db.flush()

    group_ids = {
        parsed for rule in body.rules if (parsed := safe_uuid(rule.group_id)) is not None
    }
    known_groups = set((await db.execute(select(Group.id).where(Group.id.in_(group_ids)))).scalars().all()) if group_ids else set()
    await db.execute(delete(DepartmentGroupRule))
    seen: set[uuid.UUID] = set()
    for priority, rule in enumerate(body.rules):
        group_id = safe_uuid(rule.group_id)
        department = kept.get(rule.department.strip().casefold())
        if group_id is None or group_id not in known_groups:
            raise HTTPException(status_code=422, detail=f"No group {rule.group_id!r}.")
        if department is None:
            raise HTTPException(status_code=422, detail=f"No department {rule.department!r} in the mapping.")
        if group_id in seen:
            raise HTTPException(status_code=422, detail="A group maps to one department: it is named twice.")
        seen.add(group_id)
        db.add(DepartmentGroupRule(department_id=department.id, group_id=group_id, priority=priority))

    settings_row = (
        await db.execute(select(PlatformSettings).order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc()).limit(1))
    ).scalar_one_or_none()
    if settings_row is not None:
        settings_row.department_settings = normalize_settings(body.settings.model_dump()).payload()
    if kept and not had_departments:
        # The first mapping: the history is resolved from it (then never again unasked).
        await restart_walk(db)

    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="observability",
        entity_name="Departments",
        summary=f"Saved the department mapping: {len(kept)} department(s), {len(body.rules)} group rule(s)",
        details={"departments": sorted(kept), "rules": len(body.rules), "sources": body.settings.sources},
        request=request,
    )
    await db.commit()
    invalidate_department_map()
    return await _mapping(db)


@router.get("/departments/preview")
async def preview_departments(_admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Who the saved mapping resolves to each department today, over the active people (admin-only)."""
    return await department_preview(db)


@router.post("/departments/directory")
async def import_directory_departments(
    body: DirectoryImport,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set people's directory department from an HR export, by email (admin-only).

    A sign-in whose identity provider sends the department attribute replaces
    the imported value; an empty department clears it.
    """
    if len(body.entries) > MAX_DIRECTORY_ENTRIES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_DIRECTORY_ENTRIES} entries in one import.")
    wanted = {entry.email.strip().casefold(): (entry.department or "").strip()[:200] or None for entry in body.entries}
    users: dict[str, User] = {}
    emails = list(wanted)
    for index in range(0, len(emails), 5000):
        rows = await db.execute(select(User).where(func.lower(User.email).in_(emails[index : index + 5000])))
        users.update({user.email.casefold(): user for user in rows.scalars().all()})
    updated = 0
    for email, department in wanted.items():
        user = users.get(email)
        if user is not None and user.directory_department != department:
            user.directory_department = department
            updated += 1
    unknown = sorted(email for email in wanted if email not in users)
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="observability",
        entity_name="Departments",
        summary=f"Imported the directory department of {updated} person(s)",
        details={"entries": len(wanted), "updated": updated, "unknown": len(unknown)},
        request=request,
    )
    await db.commit()
    invalidate_department_map()
    return {"entries": len(wanted), "updated": updated, "unknown_emails": unknown[:200], "unknown_count": len(unknown)}


@router.post("/departments/history")
async def apply_departments_to_history(
    body: HistoryRewrite,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Resolve the department of every stored usage again from the current mapping (admin-only).

    The trace maintenance walks the history in bounded steps; the backfill
    state says where it stands.
    """
    from app.services.tracing.attribution_backfill import restart_walk

    await restart_walk(db, rewrite=body.rewrite)
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="observability",
        entity_name="Departments",
        summary="Applied the department mapping to the stored usage" if body.rewrite else "Restarted the attribution backfill",
        request=request,
    )
    await db.commit()
    return await _backfill_state(db)


@router.get("/analytics/export")
async def export_analytics(
    metric: str = "cost",
    group_by: str | None = None,
    split_by: str | None = None,
    period: str = "30d",
    date_from: str | None = None,
    date_to: str | None = None,
    agent_ids: str | None = None,
    user_ids: str | None = None,
    group_ids: str | None = None,
    department_ids: str | None = None,
    models: str | None = None,
    sources: str | None = None,
    exclude_user_ids: str | None = None,
    exclude_group_ids: str | None = None,
    exclude_department_ids: str | None = None,
    group_kinds: str | None = None,
    complement: bool = False,
    attribution: str = Query("attributed", pattern="^(attributed|raw)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The CSV of an analytics read (``query_platform_analytics``'s arguments, lists comma-separated)."""
    from app.services.platform_insights.analytics import analytics_export_csv
    from app.services.platform_insights.scope import InsightRefusedError, load_scope

    lists = {
        "agent_ids": agent_ids,
        "user_ids": user_ids,
        "group_ids": group_ids,
        "department_ids": department_ids,
        "models": models,
        "sources": sources,
        "exclude_user_ids": exclude_user_ids,
        "exclude_group_ids": exclude_group_ids,
        "exclude_department_ids": exclude_department_ids,
        "group_kinds": group_kinds,
    }
    args: dict[str, Any] = {name: value for name, value in lists.items() if value}
    args.update(
        metric=metric,
        group_by=group_by,
        split_by=split_by,
        period=period,
        date_from=date_from,
        date_to=date_to,
        complement=complement,
        attribution=attribution,
    )
    try:
        scope = await load_scope(db, str(user.id), direct=True)
        filename, payload = await analytics_export_csv(db, scope, args)
    except InsightRefusedError as refusal:
        raise HTTPException(status_code=400, detail=refusal.message.removeprefix("Error: ")) from None
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
