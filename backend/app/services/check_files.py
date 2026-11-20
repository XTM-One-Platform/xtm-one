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

"""Check files: the pictures a tool makes for the agent to look at.

A frame or a region crop of a video, a contact sheet, a screen-insert sweep,
a slide capture, a page render: the agent judges its own work on them, the
person asked for none of them. They are stored with
``FileAttachment.source = AGENT_CHECK_FILE_SOURCE`` (``app.llm.base``), shown
as working files (one collapsed line under the reply), handed back by a
background task as files to read, and deleted
``agent_check_file_retention_days`` after they were made by the hourly
sweep (:func:`sweep`), except one a video uses (an image scene, a backdrop).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import AGENT_CHECK_FILE_SOURCE

logger = logging.getLogger(__name__)

#: Rows a sweep pass reads at most: one tick never holds a long transaction.
SWEEP_BATCH = 500
#: Most check files one tick deletes; a backlog drains on the next ticks (#768).
SWEEP_MAX_PER_TICK = 2000


def retention_days() -> int:
    """``agent_check_file_retention_days``; 0 (or below) keeps check files forever."""
    from app.core.config import settings

    return int(getattr(settings, "agent_check_file_retention_days", 0) or 0)


def cutoff(*, now: datetime | None = None, days: int | None = None) -> datetime | None:
    """The creation time before which a check file is deleted; ``None`` when they are kept forever.

    *days* is the value Settings > Storage & retention holds
    (``file_retention.days_for``); without it, the environment value.
    """
    days = retention_days() if days is None else days
    if days <= 0:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


async def expired_ids(db: AsyncSession, *, now: datetime | None = None, batch: int = SWEEP_BATCH) -> list[str]:
    """The ids of the check files past retention that no video uses, oldest first, at most *batch*."""
    from app.models.file_attachment import FileAttachment
    from app.services.file_retention import days_for
    from app.services.video_studio.retention import files_in_use

    before = cutoff(now=now, days=await days_for(db, AGENT_CHECK_FILE_SOURCE))
    if before is None:
        return []
    ids = [
        str(file_id)
        for file_id in (
            await db.execute(
                select(FileAttachment.id)
                .where(FileAttachment.source == AGENT_CHECK_FILE_SOURCE, FileAttachment.created_at < before)
                .order_by(FileAttachment.created_at)
                .limit(batch)
            )
        )
        .scalars()
        .all()
    ]
    if not ids:
        return []
    used = await files_in_use(db, ids)
    return [file_id for file_id in ids if file_id not in used]


async def sweep(db: AsyncSession, *, now: datetime | None = None, batch: int = SWEEP_BATCH) -> int:
    """Delete one batch of check files past retention, rows then objects; how many rows were deleted.

    The rows are deleted and committed first, the objects after
    (``file_cleanup.purge_storage_objects``): an object never goes from under
    a row a rollback brings back.
    """
    import uuid

    from app.services.file_cleanup import delete_file_attachment_rows, purge_storage_objects

    ids = await expired_ids(db, now=now, batch=batch)
    if not ids:
        return 0
    keys = await delete_file_attachment_rows(db, id=[uuid.UUID(file_id) for file_id in ids], source=AGENT_CHECK_FILE_SOURCE)
    await db.commit()
    await purge_storage_objects(keys)
    return len(keys)


async def sweep_pass(
    db: AsyncSession, *, now: datetime | None = None, limit: int = SWEEP_MAX_PER_TICK, batch: int = SWEEP_BATCH
) -> tuple[int, bool]:
    """Batches of :func:`sweep` up to *limit* files: ``(deleted, more left)``.

    ``more`` is True when the pass stopped at *limit* with a full last batch, so
    the scheduler runs another pass at the next tick instead of the next hour.
    A batch whose files a video all uses deletes nothing and ends the pass.
    """
    deleted = 0
    while deleted < limit:
        count = await sweep(db, now=now, batch=min(batch, limit - deleted))
        if count == 0:
            return deleted, False
        deleted += count
    return deleted, True
