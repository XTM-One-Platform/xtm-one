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

"""Task endpoints – execution history for assignment runs."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.task import Task
from app.models.user import User

router = APIRouter()


class TaskResponse(BaseModel):
    id: str
    agent_id: str | None = None
    assignment_id: str | None = None
    trigger_type: str
    status: str
    progress: int
    result: dict | None = None
    error_message: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    result = await db.execute(
        select(Task)
        .where(Task.user_id == user.id)
        .order_by(Task.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        TaskResponse(
            id=str(t.id),
            agent_id=str(t.agent_id) if t.agent_id else None,
            assignment_id=str(t.assignment_id) if t.assignment_id else None,
            trigger_type=t.trigger_type,
            status=t.status,
            progress=t.progress,
            result=t.result,
            error_message=t.error_message,
            created_at=t.created_at.isoformat(),
        )
        for t in result.scalars().all()
    ]
