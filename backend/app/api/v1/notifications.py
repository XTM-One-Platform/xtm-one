# SPDX-License-Identifier: Apache-2.0

"""Notifications API - the feed the bell and the phone read, and what is read.

``GET /notifications`` is the person's feed (``services/notification_feed.py``),
each item with the page it opens and whether it is unread. Read state is on the
server, so marking read on the computer clears the phone's badge too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services.notification_feed import (
    FEED_WINDOW,
    MAX_DISMISSED_IDS,
    WORK_ITEM_WAITING_PREFIX,
    build_feed,
    load_read_state,
    save_read_state,
)

router = APIRouter()

#: A notification id, in every form the feed emits: a run's or an activity's
#: UUID, or a waiting work item's ``work_item:<uuid>`` (#901).
NotificationId = Annotated[
    str, Field(min_length=1, max_length=74, pattern=rf"^({WORK_ITEM_WAITING_PREFIX})?[0-9a-fA-F-]{{1,64}}$")
]


class NotificationItemResponse(BaseModel):
    id: str
    kind: str
    created_at: str
    route: str
    title: str
    subtitle: str | None = None
    summary: str | None = None
    critical: bool = False
    run_id: str | None = None
    #: A decision still waiting: unread until answered, no read on open (#991).
    awaiting_answer: bool = False
    #: What a ``decision`` entry waits on: ``run``, ``flow_run`` or ``work_item`` (#842, S4).
    source: Literal["run", "flow_run", "work_item"] | None = None
    unread: bool


class NotificationFeedResponse(BaseModel):
    items: list[NotificationItemResponse]
    unread_count: int
    read_at: str | None = None


class MarkReadRequest(BaseModel):
    """Mark everything read now, or (once) bring in what a browser had marked.

    With no field: everything is read as of now. ``read_at`` / ``dismissed_ids``
    import the marker a browser kept before read state moved to the server;
    the later of the two markers wins, so an import never unreads anything.
    """

    model_config = ConfigDict(extra="forbid")

    read_at: datetime | None = None
    dismissed_ids: Annotated[list[NotificationId], Field(default_factory=list, max_length=MAX_DISMISSED_IDS)]


@router.get("", response_model=NotificationFeedResponse)
async def get_notifications(
    limit: int = Query(FEED_WINDOW, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The person's notifications: waiting ones first, then the newest."""
    return await build_feed(db, user, limit=limit)


@router.post("/read", response_model=NotificationFeedResponse)
async def mark_notifications_read(
    body: MarkReadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(UTC)
    current_read_at, current_dismissed = await load_read_state(db, user.id)
    if body.read_at is None and not body.dismissed_ids:
        # The dismissed ids are kept: they hold the read state of the kinds a
        # read marker never reads (a waiting decision, a HITL pause), so a
        # bulk read never unreads an entry read one by one before it (#901, UX B4).
        await save_read_state(db, user.id, read_at=now, dismissed=current_dismissed)
    else:
        imported = body.read_at
        if imported is not None:
            imported = imported if imported.tzinfo else imported.replace(tzinfo=UTC)
            imported = min(imported, now)
        candidates = [t for t in (current_read_at, imported) if t is not None]
        read_at = max(candidates) if candidates else None
        known = set(current_dismissed)
        dismissed = [*current_dismissed, *(i for i in dict.fromkeys(body.dismissed_ids) if i not in known)]
        await save_read_state(db, user.id, read_at=read_at, dismissed=dismissed)
    await db.commit()
    return await build_feed(db, user)


@router.post("/{notification_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark one notification read."""
    try:
        NotificationIdModel.model_validate({"id": notification_id})
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found") from exc
    read_at, dismissed = await load_read_state(db, user.id)
    if notification_id not in dismissed:
        await save_read_state(db, user.id, read_at=read_at, dismissed=[*dismissed, notification_id])
        await db.commit()


class NotificationIdModel(BaseModel):
    id: NotificationId
