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

"""Flow event-type declarations API.

CRUD for :class:`app.models.flow_event.FlowEventType` — the typed event
signatures the builder's Emit / On event / Wait for event nodes bind to
(the UE Event Dispatcher declaration).  Deliberately small: declarations are
lightweight rows, and the heavy behaviour (emission, dispatch, park/resume)
lives in ``app/services/flow_events.py``.

Same gating as the rest of the builder (agentic AI). Visibility follows the
standard three-tier model so a declaration can be shared exactly like the
flows that use it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    get_user_group_ids,
    parse_path_id,
    user_visible_filter,
    user_visible_query,
)
from app.api.v1.schemas.flow import (
    FlowEventTypeCreate,
    FlowEventTypeResponse,
    FlowEventTypeUpdate,
)
from app.core.database import get_db
from app.models.flow_event import FlowEvent, FlowEventType
from app.models.user import User
from app.services.audit import log_audit

router = APIRouter()


def _serialize(row: FlowEventType) -> FlowEventTypeResponse:
    return FlowEventTypeResponse(
        id=str(row.id),
        user_id=str(row.user_id) if row.user_id else None,
        name=row.name,
        description=row.description or "",
        fields=list(row.fields or []),
        is_company_managed=bool(row.is_company_managed),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


async def _get_editable(
    db: AsyncSession, user: User, event_type_id: str
) -> FlowEventType:
    row = (
        await db.execute(
            select(FlowEventType).where(
                FlowEventType.id == parse_path_id(event_type_id, not_found_detail="Event type not found"),
                user_visible_filter(
                    FlowEventType,
                    user.id,
                    await get_user_group_ids(db, user.id),
                    is_admin=user.is_admin,
                ),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found"
        )
    if not user.is_admin and (row.is_company_managed or row.user_id != user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can modify this event type",
        )
    return row


def _validate_field_names(payload_fields: list) -> None:
    names = [f.name for f in payload_fields]
    if len(names) != len(set(names)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Field names must be unique",
        )


@router.get("", response_model=list[FlowEventTypeResponse])
async def list_event_types(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _: None = Depends(require_agentic_ai_licensed),
) -> list[FlowEventTypeResponse]:
    rows = (
        (
            await db.execute(
                user_visible_query(
                    FlowEventType,
                    user.id,
                    await get_user_group_ids(db, user.id),
                    is_admin=user.is_admin,
                ).order_by(FlowEventType.name)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize(row) for row in rows]


@router.post(
    "", response_model=FlowEventTypeResponse, status_code=status.HTTP_201_CREATED
)
async def create_event_type(
    payload: FlowEventTypeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _: None = Depends(require_agentic_ai_licensed),
) -> FlowEventTypeResponse:
    _validate_field_names(payload.fields)
    row = FlowEventType(
        user_id=user.id,
        name=payload.name,
        description=payload.description or "",
        fields=[f.model_dump() for f in payload.fields],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await log_audit(
        db,
        user=user,
        action="flow_event_type.create",
        entity_type="flow_event_type",
        entity_id=row.id,
        entity_name=row.name,
        summary=f"Declared flow event type '{row.name}'",
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _serialize(row)


@router.put("/{event_type_id}", response_model=FlowEventTypeResponse)
async def update_event_type(
    event_type_id: str,
    payload: FlowEventTypeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _: None = Depends(require_agentic_ai_licensed),
) -> FlowEventTypeResponse:
    row = await _get_editable(db, user, event_type_id)
    changed: list[str] = []
    if payload.name is not None and payload.name != row.name:
        row.name = payload.name
        changed.append("name")
    if payload.description is not None and payload.description != (
        row.description or ""
    ):
        row.description = payload.description
        changed.append("description")
    if payload.fields is not None:
        _validate_field_names(payload.fields)
        new_fields = [f.model_dump() for f in payload.fields]
        if new_fields != list(row.fields or []):
            row.fields = new_fields
            changed.append("fields")
    await db.commit()
    await db.refresh(row)
    if changed:
        await log_audit(
            db,
            user=user,
            action="flow_event_type.update",
            entity_type="flow_event_type",
            entity_id=row.id,
            entity_name=row.name,
            summary=(
                f"Updated flow event type '{row.name}' ({', '.join(changed)})"
            ),
        )
        # Commit before answering: the get_db teardown only commits after the response is sent.
        await db.commit()
    return _serialize(row)


@router.delete("/{event_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event_type(
    event_type_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _: None = Depends(require_agentic_ai_licensed),
) -> None:
    row = await _get_editable(db, user, event_type_id)
    # Emissions cascade with the declaration (FK ON DELETE CASCADE); nodes
    # still bound to it fail at run time with a clear "pick another" error.
    await db.execute(
        FlowEvent.__table__.delete().where(FlowEvent.event_type_id == row.id)
    )
    await db.delete(row)
    await db.commit()
    await log_audit(
        db,
        user=user,
        action="flow_event_type.delete",
        entity_type="flow_event_type",
        entity_id=event_type_id,
        entity_name=row.name,
        summary=f"Deleted flow event type '{row.name}'",
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
