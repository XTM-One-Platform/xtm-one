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

"""Feedback criteria, Settings > Agents and automations > Quality.

Admins define the dimensions a rating of agent work is scored on; everyone who
rates reads the enabled ones, since the rating dialog draws one input per
criterion. Every rule about what a definition may be lives in
``services.feedback_criteria`` - this router validates the request shape,
applies the change, and audits it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.models.feedback_criterion import FeedbackCriterion
from app.models.user import User
from app.services.audit import log_audit
from app.services.feedback_criteria import (
    CriterionError,
    CriterionType,
    assert_weights_usable,
    base_key,
    load_criteria,
    unique_key,
    validate_shape,
    validate_weight,
)

router = APIRouter()

_ENTITY_TYPE = "feedback_criterion"
_NOT_FOUND = "Criterion not found"


class CriterionOption(BaseModel):
    value: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=100)
    score: float = Field(ge=0, le=1)


class CriterionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    type: CriterionType
    weight: float = Field(default=1.0, ge=0)
    options: list[CriterionOption] | None = None
    min_value: float | None = None
    max_value: float | None = None
    sort_order: int = 0
    enabled: bool = True


class CriterionUpdate(BaseModel):
    """Every field optional; ``type`` (and the key) cannot change once scores exist
    against it, so it is not accepted here at all."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    weight: float | None = Field(default=None, ge=0)
    options: list[CriterionOption] | None = None
    min_value: float | None = None
    max_value: float | None = None
    sort_order: int | None = None
    enabled: bool | None = None


class CriterionResponse(BaseModel):
    id: str
    key: str
    name: str
    description: str | None = None
    type: CriterionType
    weight: float
    options: list[CriterionOption] | None = None
    min_value: float | None = None
    max_value: float | None = None
    sort_order: int
    enabled: bool
    created_at: str
    updated_at: str


#: Fields of ``CriterionUpdate`` that may not be sent as an explicit null: the
#: column behind them is NOT NULL, and a null that silently meant "no change"
#: would hide a client bug.
_NON_NULLABLE_UPDATE_FIELDS = ("name", "weight", "sort_order", "enabled")


def _to_response(row: FeedbackCriterion) -> CriterionResponse:
    return CriterionResponse(
        id=str(row.id),
        key=row.key,
        name=row.name,
        description=row.description,
        type=row.type,
        weight=row.weight,
        options=[CriterionOption(**option) for option in row.options] if row.options else None,
        min_value=row.min_value,
        max_value=row.max_value,
        sort_order=row.sort_order,
        enabled=row.enabled,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _unprocessable(exc: CriterionError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def _assert_name_free(db: AsyncSession, name: str, *, exclude_id: uuid.UUID | None = None) -> None:
    query = select(func.count()).select_from(FeedbackCriterion).where(
        func.lower(FeedbackCriterion.name) == name.lower()
    )
    if exclude_id is not None:
        query = query.where(FeedbackCriterion.id != exclude_id)
    if int(await db.scalar(query) or 0) > 0:
        raise HTTPException(status_code=409, detail=f"A criterion named {name!r} already exists.")


async def _get_criterion(db: AsyncSession, criterion_id: uuid.UUID) -> FeedbackCriterion:
    row = await db.get(FeedbackCriterion, criterion_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return row


def _options_payload(options: list[CriterionOption] | None) -> list[dict[str, Any]] | None:
    return [option.model_dump() for option in options] if options is not None else None


@router.get("", response_model=list[CriterionResponse])
async def list_feedback_criteria(
    include_disabled: bool = Query(False, description="Admins only: disabled criteria too"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The criteria the rating dialog offers; an admin may ask for the disabled ones."""
    rows = await load_criteria(db, include_disabled=include_disabled and user.is_admin)
    return [_to_response(row) for row in rows]


@router.post("", response_model=CriterionResponse, status_code=status.HTTP_201_CREATED)
async def create_feedback_criterion(
    body: CriterionCreate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A criterion needs a name.")
    try:
        options, min_value, max_value = validate_shape(
            body.type, _options_payload(body.options), body.min_value, body.max_value
        )
        weight = validate_weight(body.weight)
        existing = await load_criteria(db, include_disabled=True)
        assert_weights_usable(
            [row.weight for row in existing if row.enabled] + ([weight] if body.enabled else [])
        )
    except CriterionError as exc:
        raise _unprocessable(exc) from exc
    await _assert_name_free(db, name)

    row = FeedbackCriterion(
        key=unique_key(base_key(name), {criterion.key for criterion in existing}),
        name=name,
        description=(body.description or "").strip() or None,
        type=body.type,
        weight=weight,
        options=options,
        min_value=min_value,
        max_value=max_value,
        sort_order=body.sort_order,
        enabled=body.enabled,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        # A concurrent create of the same name took the key first.
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"A criterion named {name!r} already exists.") from exc
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type=_ENTITY_TYPE,
        entity_id=row.id,
        entity_name=row.name,
        summary=f"Created feedback criterion '{row.name}'",
        details={"type": row.type, "weight": row.weight, "enabled": row.enabled},
    )
    await db.commit()
    return _to_response(row)


@router.put("/{criterion_id}", response_model=CriterionResponse)
async def update_feedback_criterion(
    criterion_id: str,
    body: CriterionUpdate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cid = parse_path_id(criterion_id, not_found_detail=_NOT_FOUND)
    sent = body.model_fields_set
    for field_name in _NON_NULLABLE_UPDATE_FIELDS:
        if field_name in sent and getattr(body, field_name) is None:
            raise HTTPException(status_code=422, detail=f"{field_name} cannot be null.")

    row = await _get_criterion(db, cid)
    name = body.name.strip() if "name" in sent and body.name is not None else row.name
    if not name:
        raise HTTPException(status_code=422, detail="A criterion needs a name.")
    try:
        options, min_value, max_value = validate_shape(
            row.type,
            _options_payload(body.options) if "options" in sent else row.options,
            body.min_value if "min_value" in sent else row.min_value,
            body.max_value if "max_value" in sent else row.max_value,
        )
        weight = validate_weight(body.weight) if "weight" in sent else row.weight
        enabled = body.enabled if "enabled" in sent else row.enabled
        others = [
            other for other in await load_criteria(db, include_disabled=True) if other.id != row.id
        ]
        assert_weights_usable(
            [other.weight for other in others if other.enabled] + ([weight] if enabled else [])
        )
    except CriterionError as exc:
        raise _unprocessable(exc) from exc
    if name.lower() != row.name.lower():
        await _assert_name_free(db, name, exclude_id=row.id)

    row.name = name
    if "description" in sent:
        row.description = (body.description or "").strip() or None
    row.options = options
    row.min_value = min_value
    row.max_value = max_value
    row.weight = weight
    row.enabled = bool(enabled)
    if "sort_order" in sent and body.sort_order is not None:
        row.sort_order = body.sort_order
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type=_ENTITY_TYPE,
        entity_id=row.id,
        entity_name=row.name,
        summary=f"Updated feedback criterion '{row.name}'",
        details={"fields": sorted(sent)},
    )
    await db.commit()
    return _to_response(row)


@router.delete("/{criterion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback_criterion(
    criterion_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a criterion and every score given on it. Disabling keeps the history."""
    cid = parse_path_id(criterion_id, not_found_detail=_NOT_FOUND)
    row = await _get_criterion(db, cid)
    try:
        assert_weights_usable(
            other.weight
            for other in await load_criteria(db, include_disabled=True)
            if other.enabled and other.id != row.id
        )
    except CriterionError as exc:
        raise _unprocessable(exc) from exc
    name = row.name
    await db.delete(row)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type=_ENTITY_TYPE,
        entity_id=cid,
        entity_name=name,
        summary=f"Deleted feedback criterion '{name}'",
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
