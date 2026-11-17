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

"""Presets of fleet member settings: the company list and its administration.

``/api/v1/fleet-member-presets`` (dev-docs/ctem-fleets.md section 10.6, R4 of
#9162). Every signed-in user reads the list (the built-ins first) to apply a
preset in a fleet's Members view; only an administrator creates, renames,
changes or deletes a stored preset (decision R-D3 A). The built-ins are read
only. Applying a preset is not a route here: it is the write it stands for,
``PATCH /fleets/{id}`` (member defaults) or ``POST /fleets/{id}/members/bulk``.
The rules live in ``app.services.fleet_member_presets``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai_licensed
from app.api.v1.schemas.fleet import FleetMemberPresetCreate, FleetMemberPresetResponse, FleetMemberPresetUpdate
from app.core.database import get_db
from app.models.user import User
from app.services import fleet_member_presets as presets
from app.services.audit import log_audit
from app.services.fleets import FleetActionRefusedError

router = APIRouter()


def _response(view: presets.PresetView) -> FleetMemberPresetResponse:
    return FleetMemberPresetResponse(id=view.id, name=view.name, values=view.values, builtin=view.builtin)


def _refused(exc: FleetActionRefusedError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("", response_model=list[FleetMemberPresetResponse])
async def list_fleet_member_presets(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> list[FleetMemberPresetResponse]:
    """The built-in presets, then the company's, by name."""
    return [_response(v) for v in await presets.list_presets(db)]


@router.post("", response_model=FleetMemberPresetResponse, status_code=201)
async def create_fleet_member_preset(
    body: FleetMemberPresetCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetMemberPresetResponse:
    """Store a company preset; 409 on a name taken, 422 names a value refused."""
    try:
        row = await presets.create_preset(
            db, name=body.name, values=body.values.model_dump(exclude_unset=True), user_id=admin.id
        )
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="fleet_member_preset",
        entity_id=row.id,
        entity_name=row.name,
        summary=f"Created the fleet member preset '{row.name}'",
        details={"values": row.values},
        request=request,
    )
    await db.commit()
    await db.refresh(row)
    return _response(presets.preset_view(row))


@router.patch("/{preset_id}", response_model=FleetMemberPresetResponse)
async def update_fleet_member_preset(
    preset_id: str,
    body: FleetMemberPresetUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetMemberPresetResponse:
    """Rename a stored preset or replace its values; a built-in answers 403."""
    values = body.values.model_dump(exclude_unset=True) if body.values is not None else None
    try:
        row = await presets.update_preset(db, preset_id, name=body.name, values=values)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="fleet_member_preset",
        entity_id=row.id,
        entity_name=row.name,
        summary=f"Changed the fleet member preset '{row.name}'",
        details={"values": row.values},
        request=request,
    )
    await db.commit()
    await db.refresh(row)
    return _response(presets.preset_view(row))


@router.delete("/{preset_id}", status_code=204)
async def delete_fleet_member_preset(
    preset_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> Response:
    """Delete a stored preset; no fleet changes. A built-in answers 403."""
    try:
        name = await presets.delete_preset(db, preset_id)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="fleet_member_preset",
        entity_name=name,
        summary=f"Deleted the fleet member preset '{name}'",
        details={"preset_id": preset_id},
        request=request,
    )
    await db.commit()
    return Response(status_code=204)
