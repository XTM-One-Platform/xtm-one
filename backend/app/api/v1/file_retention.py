# SPDX-License-Identifier: Apache-2.0

"""Settings > Storage & retention: how long each category of stored file is kept.

``GET``   the rules per category (days, unit, basis, lock), what each holds
          today (files, bytes, tombstones), the next sweep and the last one.
``PUT``   change the days of one or more categories (administrators); an
          audit log entry names every change. A category the environment sets
          is locked (``409``).
``POST /preview`` a dry run: what the next sweep would delete per category
          under the days given, nothing written.

The rules and the sweep live in ``app.services.file_retention``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.database import get_db
from app.models.user import User
from app.services import file_retention, wall_clock_schedule
from app.services.audit import log_audit

router = APIRouter()


class FileRetentionWrite(BaseModel):
    """The days to store per category key; 0 = never deleted."""

    days: dict[str, int] = Field(default_factory=dict)


async def _payload(db: AsyncSession, user: User) -> dict[str, Any]:
    rules = await file_retention.load_policy(db)
    held = await file_retention.holdings(db)
    # The sweep rides the hourly maintenance pass: its next run is the last
    # COMPLETED pass plus the interval, read from the persisted schedule
    # (``None``: due now, it runs at the next scheduler tick), never a clock
    # computed from now (#768).
    schedule = await wall_clock_schedule.status(wall_clock_schedule.HOURLY_MAINTENANCE)
    return {
        "categories": [
            {**rules[c.key].as_dict(), **held.get(c.key, {}), "unit": "days"} for c in file_retention.CATEGORIES
        ],
        "max_days": file_retention.MAX_DAYS,
        "next_sweep_at": schedule.next_due_at.isoformat() if schedule.next_due_at else None,
        "sweep_overdue": schedule.overdue,
        "maintenance_completed_at": (schedule.last_completed_at.isoformat() if schedule.last_completed_at else None),
        "last_sweep": await file_retention.last_sweep(),
        "can_edit": bool(getattr(user, "is_admin", False)),
    }


@router.get("")
async def get_file_retention(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Every file retention rule, with what each category holds today (administrators)."""
    return await _payload(db, user)


@router.put("")
async def save_file_retention(
    body: FileRetentionWrite,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Change the days of the categories given (administrators); an audit entry names each change."""
    try:
        changed = await file_retention.save_policy(db, body.days)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown file category: {exc.args[0]}") from None
    except file_retention.RetentionLockedError as exc:
        category = file_retention.BY_KEY[str(exc)]
        raise HTTPException(
            status_code=409,
            detail=f"{category.key} is set by the environment variable {category.env_var}: change it there.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if changed:
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="platform_settings",
            entity_name="File retention",
            summary="Changed the file retention",
            details={"file_retention": changed},
            request=request,
        )
    await db.commit()
    return await _payload(db, user)


@router.post("/preview")
async def preview_file_retention(
    body: FileRetentionWrite,
    _user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What the next sweep would delete per category under *body* (the stored days elsewhere); nothing written."""
    for key, days in body.days.items():
        if key not in file_retention.BY_KEY:
            raise HTTPException(status_code=422, detail=f"Unknown file category: {key}")
        if isinstance(days, bool) or not 0 <= days <= file_retention.MAX_DAYS:
            raise HTTPException(status_code=422, detail=f"{key}: 0 to {file_retention.MAX_DAYS} days")
    report = await file_retention.preview(db, body.days)
    await db.rollback()
    return {
        "categories": [
            {
                "category": key,
                "files": int(t["files"]),
                "bytes": int(t["bytes"]),
                "kept_referenced": int(t["kept_referenced"]),
                "more": bool(t["more"]),
            }
            for key, t in report.items()
        ],
        "total_files": sum(int(t["files"]) for t in report.values()),
        "total_bytes": sum(int(t["bytes"]) for t in report.values()),
    }


@router.get("/rules")
async def read_file_retention_rules(
    _user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The rules alone (days per category), for any signed-in person or agent: what a file's card says."""
    rules = await file_retention.load_policy(db)
    return {"categories": [rules[c.key].as_dict() for c in file_retention.CATEGORIES]}
