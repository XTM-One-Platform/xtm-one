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

"""Dashboard widget packs API - the library of ready-made dashboard widgets.

Behind ``DASHBOARD_STUDIO`` (a 404 while it is off). The rules are
``services/dashboard_widget_packs.py``'s: three-tier visibility; a pack is
changed, shared and deleted by its managers, never a built-in (duplicate it);
an import is a file the platform parses (an XTM One pack, an OpenCTI dashboard
or widget export, an OpenAEV dashboard export, at most 5 MB) into widgets
checked like a dashboard's, with a report of what changed; inserting writes
the chosen widgets into a dashboard the person may edit, each platform widget
bound to an integration of its platform they can use.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.core.feature_flags import require_feature_or_404
from app.models.user import User
from app.services.audit import log_audit
from app.services.custom_dashboard_spec import DashboardSpecError
from app.services.custom_dashboards import DashboardRevisionConflictError, load_dashboard_for, serialize_dashboard
from app.services.dashboard_interop import MAX_FILE_BYTES
from app.services.dashboard_widget_packs import (
    MAX_PACK_DESCRIPTION,
    MAX_PACK_NAME,
    PackError,
    import_pack,
    insert_pack_widgets,
    list_visible_packs,
    load_pack_for,
    pack_from_dashboard,
    serialize_pack,
)
from app.services.share_principals import SharingChange

#: 404 while the Dashboard Studio is off: these routes are the studio's.
_require_studio = require_feature_or_404("DASHBOARD_STUDIO")


router = APIRouter(dependencies=[Depends(_require_studio)])

_NOT_FOUND = "Widget pack not found"


class PackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=MAX_PACK_NAME)] = None
    description: Annotated[str | None, Field(default=None, max_length=MAX_PACK_DESCRIPTION)] = None


class PackSharingUpdate(SharingChange):
    model_config = ConfigDict(extra="forbid")


class PackFromDashboard(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dashboard_id: str
    name: Annotated[str, Field(min_length=1, max_length=MAX_PACK_NAME)]
    description: Annotated[str | None, Field(default=None, max_length=MAX_PACK_DESCRIPTION)] = None
    widget_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]] | None, Field(max_length=64)] = None


class PackInsert(BaseModel):
    """Insert a pack's widgets into a dashboard, at the revision the client read."""

    model_config = ConfigDict(extra="forbid")

    dashboard_id: str
    revision: Annotated[int, Field(ge=1)]
    widget_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]] | None, Field(max_length=64)] = None
    #: The integration each platform reads through (``{"opencti": "<id>"}``); one only: implied.
    bindings: dict[Literal["opencti", "openaev", "opencrq"], str] | None = None
    after: Annotated[str | None, Field(default=None, min_length=1, max_length=40)] = None


async def _pack(db: AsyncSession, user: User, pack_id: str, *, need: Literal["read", "manage"] = "read"):
    pid = parse_path_id(pack_id, not_found_detail=_NOT_FOUND)
    found = await load_pack_for(db, pid, user_id=user.id, is_admin=user.is_admin)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if need == "manage" and not found[1]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A built-in pack is not changed: duplicate it" if "builtin" in (found[0].tags or []) else "You can use this pack but not change it",
        )
    return found


@router.get("")
async def list_widget_packs(
    scope: Literal["all", "mine", "shared", "company", "builtin"] = Query(default="all"),
    source: Literal["opencti", "openaev", "opencrq"] | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The packs the caller may use: built-ins first, then the most recent."""
    rows = await list_visible_packs(db, user_id=user.id, is_admin=user.is_admin, scope=scope, source=source)
    return [serialize_pack(pack, can_manage=manage) for pack, manage in rows]


@router.get("/integrations")
async def list_pack_integrations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The integrations the caller can bind a pack's widgets to, and the XTM Hubs they can share on."""
    from app.services.platform_access import RESOLVABLE_PROVIDERS, visible_platform_integrations

    rows = await visible_platform_integrations(db, user.id, providers=RESOLVABLE_PROVIDERS)
    return [{"id": str(row.id), "provider": row.provider, "name": row.name} for row in rows]


@router.get("/{pack_id}")
async def get_widget_pack(pack_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """One pack with its widgets."""
    pack, manage = await _pack(db, user, pack_id)
    return serialize_pack(pack, can_manage=manage, include_widgets=True)


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_widget_pack(
    request: Request,
    file: UploadFile = File(...),
    name: Annotated[str | None, Form(max_length=MAX_PACK_NAME)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a pack from a file; the answer lists what the conversion changed or left out."""
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is larger than 5 MB.")
    try:
        pack = await import_pack(db, user_id=user.id, data=data, filename=file.filename or "", name=name)
    except PackError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await log_audit(
        db, user=user, action="create", entity_type="dashboard_widget_pack", entity_id=pack.id, entity_name=pack.name,
        summary=f"Imported widget pack '{pack.name}'", details={"origin": pack.origin, "widgets": pack.widget_count},
        request=request,
    )  # fmt: skip
    await db.commit()
    return serialize_pack(pack, can_manage=True, include_widgets=True)


@router.post("/from-dashboard", status_code=status.HTTP_201_CREATED)
async def save_dashboard_as_pack(
    body: PackFromDashboard,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A pack of a dashboard's widgets (whoever reads the dashboard); platform widgets lose their integration."""
    dashboard_uuid = parse_path_id(body.dashboard_id, not_found_detail="Dashboard not found")
    found = await load_dashboard_for(db, dashboard_uuid, user_id=user.id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")
    try:
        pack = await pack_from_dashboard(
            db, user_id=user.id, dashboard=found[0], name=body.name, description=body.description, widget_ids=body.widget_ids
        )
    except PackError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await log_audit(
        db, user=user, action="create", entity_type="dashboard_widget_pack", entity_id=pack.id, entity_name=pack.name,
        summary=f"Saved widget pack '{pack.name}' from a dashboard", details={"widgets": pack.widget_count},
        request=request,
    )  # fmt: skip
    await db.commit()
    return serialize_pack(pack, can_manage=True, include_widgets=True)


@router.post("/{pack_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_widget_pack(
    pack_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """A private copy of a pack the caller may use (how a built-in is changed)."""
    from app.core.utils import fit_copy_name
    from app.models.dashboard_widget_pack import DashboardWidgetPack
    from app.services.dashboard_widget_packs import create_pack

    pack, _manage = await _pack(db, user, pack_id)
    copy = await create_pack(
        db, user_id=user.id, name=fit_copy_name(pack.name, DashboardWidgetPack.__table__.c.name, suffix=" (copy)"),
        description=pack.description,
        widgets=list(pack.widgets or []), origin=pack.origin if pack.origin != "builtin" else "upload",
        inputs=list(pack.inputs or []),
    )  # fmt: skip
    await log_audit(
        db, user=user, action="create", entity_type="dashboard_widget_pack", entity_id=copy.id, entity_name=copy.name,
        summary=f"Duplicated widget pack '{pack.name}'", details={"from": str(pack.id)}, request=request,
    )  # fmt: skip
    await db.commit()
    return serialize_pack(copy, can_manage=True, include_widgets=True)


@router.patch("/{pack_id}")
async def update_widget_pack(
    pack_id: str,
    body: PackUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pack, _manage = await _pack(db, user, pack_id, need="manage")
    if body.name is not None:
        pack.name = body.name
    if "description" in body.model_fields_set:
        pack.description = body.description or None
    await log_audit(
        db, user=user, action="update", entity_type="dashboard_widget_pack", entity_id=pack.id, entity_name=pack.name,
        summary=f"Updated widget pack '{pack.name}'", request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(pack)
    return serialize_pack(pack, can_manage=True)


@router.put("/{pack_id}/sharing")
async def share_widget_pack(
    pack_id: str,
    body: PackSharingUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change who may use the pack (its managers); making it a company pack is an admin's act."""
    from app.services.presentation_sharing import apply_sharing

    pack, _manage = await _pack(db, user, pack_id, need="manage")
    await apply_sharing(db, user, pack, body)
    await log_audit(
        db, user=user, action="update", entity_type="dashboard_widget_pack", entity_id=pack.id, entity_name=pack.name,
        summary=f"Changed who can use widget pack '{pack.name}'", request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(pack)
    found = await load_pack_for(db, pack.id, user_id=user.id, is_admin=user.is_admin)
    return serialize_pack(pack, can_manage=bool(found and found[1]))


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_widget_pack(
    pack_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    pack, _manage = await _pack(db, user, pack_id, need="manage")
    await log_audit(
        db, user=user, action="delete", entity_type="dashboard_widget_pack", entity_id=pack.id, entity_name=pack.name,
        summary=f"Deleted widget pack '{pack.name}'", request=request,
    )  # fmt: skip
    await db.delete(pack)
    await db.commit()


@router.get("/{pack_id}/export")
async def export_widget_pack(pack_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The pack as an XTM One pack file (``xtm-one-widget-pack`` v1)."""
    from app.services.dashboard_interop.pack_format import build_pack

    pack, _manage = await _pack(db, user, pack_id)
    document = build_pack(pack.name, pack.description, list(pack.widgets or []), list(pack.inputs or []))
    return Response(
        content=json.dumps(document, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="widget-pack.json"'},
    )


@router.post("/{pack_id}/insert")
async def insert_widget_pack(
    pack_id: str,
    body: PackInsert,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Insert the pack's widgets into a dashboard the caller may edit."""
    pack, _manage = await _pack(db, user, pack_id)
    dashboard_uuid = parse_path_id(body.dashboard_id, not_found_detail="Dashboard not found")
    found = await load_dashboard_for(db, dashboard_uuid, user_id=user.id, need="edit", for_update=True)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")
    dashboard = found[0]
    try:
        inserted = await insert_pack_widgets(
            db, user_id=user.id, dashboard=dashboard, pack=pack, widget_ids=body.widget_ids,
            bindings=dict(body.bindings or {}), after=body.after, expected_revision=body.revision,
        )  # fmt: skip
    except DashboardRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (PackError, DashboardSpecError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await log_audit(
        db, user=user, action="update", entity_type="custom_dashboard", entity_id=dashboard.id,
        entity_name=dashboard.title, summary=f"Inserted {len(inserted)} widgets of pack '{pack.name}'",
        details={"pack_id": str(pack.id), "revision": dashboard.revision}, request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(dashboard)
    return {"inserted": inserted, "dashboard": serialize_dashboard(dashboard)}
