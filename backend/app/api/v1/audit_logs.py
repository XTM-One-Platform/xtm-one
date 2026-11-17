# SPDX-License-Identifier: Apache-2.0

"""Audit log endpoints – searchable platform-wide audit trail (admin-only)."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.v1.helpers import parse_uuid
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User


def _parse_iso_datetime(value: str, *, label: str) -> datetime:
    """Parse an ISO-8601 datetime, raising 422 on bad input.

    A naive value is assumed to be UTC; a value carrying an explicit offset is
    preserved and normalized to UTC (the previous ``.replace(tzinfo=UTC)`` threw
    on non-ISO input and silently shifted the instant of offset-aware values).
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid {label}: {value!r}"
        ) from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


#: The columns of the audit log table the API sorts on (``sort=``), each with
#: the column it orders by; ``id`` breaks every tie so offset pages never overlap.
AUDIT_LOG_SORTS: dict[str, Any] = {
    "time": AuditLog.created_at,
    "action": AuditLog.action,
    "entity": AuditLog.entity_type,
    "user": func.lower(AuditLog.user_email),
}


def audit_log_order(sort: str | None, direction: str | None) -> list[Any]:
    """The ORDER BY of a page: the column of ``sort`` (time when unknown or absent),
    descending unless ``direction`` is ``asc``, then the id the same way."""
    column = AUDIT_LOG_SORTS.get(sort or "", AuditLog.created_at)
    if direction == "asc":
        return [column.asc(), AuditLog.id.asc()]
    return [column.desc(), AuditLog.id.desc()]


def filter_values(raw: str | None) -> list[str]:
    """The values of a filter given once or comma-separated (``action=create,delete``)."""
    if not raw:
        return []
    return [value for value in (part.strip() for part in raw.split(",")) if value]


router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuditLogResponse(BaseModel):
    id: str
    user_id: str
    user_email: str
    action: str
    entity_type: str
    entity_id: str | None = None
    entity_name: str | None = None
    summary: str
    details: dict | None = None
    ip_address: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


# ---------------------------------------------------------------------------
# Routes (all admin-only)
# ---------------------------------------------------------------------------


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(
        None, description="Search in summary, entity name, or user email"
    ),
    user_id: str | None = Query(
        None, description="Filter by user ID (several comma-separated)"
    ),
    action: str | None = Query(
        None,
        description="Filter by action (create, update, delete, etc.; several comma-separated)",
    ),
    entity_type: str | None = Query(
        None,
        description="Filter by entity type (agent, assignment, etc.; several comma-separated)",
    ),
    date_from: str | None = Query(
        None, description="Filter logs created on or after this ISO-8601 datetime"
    ),
    date_to: str | None = Query(
        None, description="Filter logs created on or before this ISO-8601 datetime"
    ),
    sort: str | None = Query(
        None, description="Sort column: time (default), action, entity or user"
    ),
    direction: str | None = Query(
        None, description="Sort direction: desc (default) or asc"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List audit logs with search and filtering (admin-only)."""
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    # Apply filters
    user_ids = [parse_uuid(value, label="user ID") for value in filter_values(user_id)]
    if user_ids:
        query = query.where(AuditLog.user_id.in_(user_ids))
        count_query = count_query.where(AuditLog.user_id.in_(user_ids))

    actions = filter_values(action)
    if actions:
        query = query.where(AuditLog.action.in_(actions))
        count_query = count_query.where(AuditLog.action.in_(actions))

    entity_types = filter_values(entity_type)
    if entity_types:
        query = query.where(AuditLog.entity_type.in_(entity_types))
        count_query = count_query.where(AuditLog.entity_type.in_(entity_types))

    if date_from:
        dt_from = _parse_iso_datetime(date_from, label="date_from")
        query = query.where(AuditLog.created_at >= dt_from)
        count_query = count_query.where(AuditLog.created_at >= dt_from)

    if date_to:
        dt_to = _parse_iso_datetime(date_to, label="date_to")
        query = query.where(AuditLog.created_at <= dt_to)
        count_query = count_query.where(AuditLog.created_at <= dt_to)

    if search:
        pattern = f"%{search}%"
        search_filter = (
            AuditLog.summary.ilike(pattern)
            | AuditLog.entity_name.ilike(pattern)
            | AuditLog.user_email.ilike(pattern)
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # Get total count
    total = await db.scalar(count_query) or 0

    # Get paginated results
    result = await db.execute(
        query.order_by(*audit_log_order(sort, direction)).limit(limit).offset(offset)
    )
    logs = result.scalars().all()

    return AuditLogListResponse(
        items=[
            AuditLogResponse(
                id=str(log.id),
                user_id=str(log.user_id),
                user_email=log.user_email,
                action=log.action,
                entity_type=log.entity_type,
                entity_id=str(log.entity_id) if log.entity_id else None,
                entity_name=log.entity_name,
                summary=log.summary,
                details=log.details,
                ip_address=log.ip_address,
                created_at=log.created_at.isoformat(),
            )
            for log in logs
        ],
        total=total,
    )


@router.get("/actions")
async def list_actions(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List distinct action types for filter dropdowns."""
    result = await db.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    return [row[0] for row in result.all()]


@router.get("/entity-types")
async def list_entity_types(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List distinct entity types for filter dropdowns."""
    result = await db.execute(
        select(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type)
    )
    return [row[0] for row in result.all()]
