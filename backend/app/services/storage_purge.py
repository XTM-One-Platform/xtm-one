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

"""On-demand purge of the platform's working storage.

The hourly sweeps delete check files after ``agent_check_file_retention_days``
(``check_files``), expire unused Video Studio media after
``video_studio_unused_media_retention_days`` / ``video_studio_media_retention_days``
(``video_studio.retention``) and drop figure stills after
``video_studio_figure_still_retention_days``. A storage spike needs them now,
not in a week: :func:`purge` applies the same rules with an age threshold the
administrator chooses, ``dry_run`` first.

What it never touches, whatever the threshold:

- a file a video needs (``retention.files_in_use``: an image scene, a
  backdrop, what a ready render drew);
- a recording or AI shot a video's timeline names, one marked "Keep its
  media", one a repair is made from, one not ready (recording, failed: the
  regular sweep's grace handles those);
- a video's own renders, posters and captions (not a category here);
- anything made in the last :data:`FLOOR` (work in flight).

Categories:

- ``agent_check``: check files (``FileAttachment.source == "agent_check"``):
  frames, contact sheets, insert crops, screenshots an agent looked at.
- ``unused_recording`` / ``unused_shot``: the media of a ready recording or AI
  shot no video uses, older than the threshold: its video, keyframes, poster,
  contact sheet and kept frames. The row stays in the library, expired, like
  the regular expiry.
- ``figure_stills``: the editor's figure stills (objects only, drawn again on
  the next preview that misses one; their size is not listed by the store).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CATEGORIES = ("agent_check", "unused_recording", "unused_shot", "figure_stills")
#: Nothing younger is ever purged: a file or a medium a run is still making or about to read.
FLOOR = timedelta(hours=1)
#: Rows read and deleted per transaction.
BATCH = 500
#: Default and most items one call purges per category; a call that stops there says ``more``.
DEFAULT_LIMIT = 5_000
MOST_LIMIT = 50_000


def cutoff(older_than_days: float, *, now: datetime | None = None) -> datetime:
    """The creation time before which an item is purged: *older_than_days* ago, never later than :data:`FLOOR` ago."""
    moment = now or datetime.now(UTC)
    return min(moment - timedelta(days=max(0.0, float(older_than_days))), moment - FLOOR)


async def _sizes(db: AsyncSession, file_ids: Iterable[object]) -> tuple[int, int]:
    """``(files, bytes)`` of the stored rows among *file_ids* (a file already gone counts for nothing)."""
    from sqlalchemy import func

    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment

    ids = list({fid for fid in (safe_uuid(str(f)) for f in file_ids if f) if fid is not None})
    files = size = 0
    for start in range(0, len(ids), BATCH):
        count, total = (
            await db.execute(
                select(func.count(FileAttachment.id), func.coalesce(func.sum(FileAttachment.size), 0)).where(
                    FileAttachment.id.in_(ids[start : start + BATCH])
                )
            )
        ).one()
        files, size = files + int(count), size + int(total)
    return files, size


async def _agent_check(db: AsyncSession, before: datetime, *, dry_run: bool, limit: int) -> dict[str, Any]:
    """Check files made before *before* that no video uses, oldest first, at most *limit*."""
    from app.llm.base import AGENT_CHECK_FILE_SOURCE
    from app.models.file_attachment import FileAttachment
    from app.services.file_cleanup import delete_file_attachment_rows, purge_storage_objects
    from app.services.video_studio.retention import files_in_use

    tally = {"count": 0, "bytes": 0, "kept_in_use": 0, "objects_left": 0, "more": False}
    after: tuple[datetime, uuid.UUID] | None = None
    while tally["count"] < limit:
        query = select(FileAttachment.id, FileAttachment.created_at, FileAttachment.size).where(
            FileAttachment.source == AGENT_CHECK_FILE_SOURCE, FileAttachment.created_at < before
        )
        if after is not None:
            query = query.where(tuple_(FileAttachment.created_at, FileAttachment.id) > after)
        rows = (
            await db.execute(query.order_by(FileAttachment.created_at, FileAttachment.id).limit(min(BATCH, limit - tally["count"])))
        ).all()
        if not rows:
            break
        after = (rows[-1][1], rows[-1][0])
        used = await files_in_use(db, [row[0] for row in rows])
        tally["kept_in_use"] += len(used)
        wanted = [row for row in rows if str(row[0]) not in used]
        tally["count"] += len(wanted)
        tally["bytes"] += sum(int(row[2] or 0) for row in wanted)
        if dry_run or not wanted:
            continue
        keys = await delete_file_attachment_rows(db, id=[row[0] for row in wanted], source=AGENT_CHECK_FILE_SOURCE)
        await db.commit()
        tally["objects_left"] += len(keys) - await purge_storage_objects(keys)
    if tally["count"] >= limit:
        tally["more"] = True
    return tally


def _media_files(row: Any) -> list[Any]:
    """Every file a ready recording or shot owns that its expiry may delete: its media, keyframes, poster, sheet, frames."""
    from app.models.video_studio import VideoTake

    if isinstance(row, VideoTake):
        from app.services.video_studio.take_repair import keyframes_file
        from app.services.video_studio.take_window import frame_list_file

        return [
            row.file_id,
            row.poster_file_id,
            row.contact_sheet_file_id,
            keyframes_file(row.capture),
            frame_list_file(row.capture),
        ]
    from app.services.video_studio import clips

    return clips.owned_files(row)


async def _unused_media(
    db: AsyncSession, kind: str, before: datetime, *, now: datetime, dry_run: bool, limit: int
) -> dict[str, Any]:
    """The ready recordings (*kind* ``take``) or AI shots (``clip``) no video uses, not kept, unused since *before*."""
    from sqlalchemy import or_

    from app.models.video_studio import JOB_READY, VideoClip, VideoTake
    from app.services.video_studio import assets, retention

    model: type[VideoTake] | type[VideoClip] = VideoTake if kind == "take" else VideoClip
    tally = {"count": 0, "files": 0, "bytes": 0, "kept_in_use": 0, "more": False}
    await retention.index_stale_refs(db)
    repairing = await retention.repaired_now(db) if kind == "take" else set()
    rows: list[Any] = list(
        (
            await db.execute(
                select(model)
                .where(
                    model.status == JOB_READY,
                    model.keep.is_(False),
                    model.expired_at.is_(None),
                    model.created_at < before,
                    or_(model.last_used_at.is_(None), model.last_used_at < before),
                )
                .order_by(model.created_at)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > limit:
        tally["more"], rows = True, rows[:limit]
    files: list[Any] = []
    for row in rows:
        if str(row.id) in repairing or await retention.in_use(db, row):
            tally["kept_in_use"] += 1
            continue
        owned = [fid for fid in _media_files(row) if fid]
        files.extend(owned)
        tally["count"] += 1
        if dry_run:
            continue
        row.file_id = None
        row.poster_file_id = None
        if isinstance(row, VideoTake):
            row.contact_sheet_file_id = None
            if isinstance(row.capture, dict) and "keyframes" in row.capture:
                row.capture = {k: v for k, v in row.capture.items() if k != "keyframes"}
        row.expired_at = now
    needed = await retention.files_in_use(db, files)
    tally["files"], tally["bytes"] = await _sizes(db, [fid for fid in files if str(fid) not in needed])
    if dry_run:
        return tally
    await db.commit()
    for start in range(0, len(files), BATCH):
        await assets.delete_files(files[start : start + BATCH])
    return tally


async def _figure_stills(before: datetime, *, dry_run: bool, limit: int) -> dict[str, Any]:
    """The editor's figure stills written before *before* (objects only; drawn again when a preview misses one)."""
    from app.services.file_storage import file_storage
    from app.services.video_studio import figures

    tally: dict[str, Any] = {"count": 0, "bytes": None, "more": False}
    for name, modified in await file_storage.list_objects_modified(f"{figures.PREVIEW_PREFIX}/"):
        if modified is None:
            continue
        stamp = modified if modified.tzinfo else modified.replace(tzinfo=UTC)
        if stamp >= before:
            continue
        if tally["count"] >= limit:
            tally["more"] = True
            break
        tally["count"] += 1
        if not dry_run:
            await file_storage.delete(name)
    return tally


async def purge(
    db: AsyncSession,
    *,
    older_than_days: float,
    dry_run: bool = True,
    categories: Iterable[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Purge (or, *dry_run*, count) what each category holds older than *older_than_days*; the tally per category.

    Each category is at most *limit* items a call (``more`` says another call
    finds the rest). Commits each batch it deletes.
    """
    moment = now or datetime.now(UTC)
    before = cutoff(older_than_days, now=moment)
    wanted = [name for name in (categories or CATEGORIES) if name in CATEGORIES]
    most = max(1, min(int(limit), MOST_LIMIT))
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "older_than_days": older_than_days,
        "created_before": before.isoformat(),
        "categories": {},
    }
    for name in wanted:
        if name == "agent_check":
            tally = await _agent_check(db, before, dry_run=dry_run, limit=most)
        elif name == "unused_recording":
            tally = await _unused_media(db, "take", before, now=moment, dry_run=dry_run, limit=most)
        elif name == "unused_shot":
            tally = await _unused_media(db, "clip", before, now=moment, dry_run=dry_run, limit=most)
        else:
            tally = await _figure_stills(before, dry_run=dry_run, limit=most)
        report["categories"][name] = tally
    tallies = report["categories"].values()
    report["total_bytes"] = sum(int(t.get("bytes") or 0) for t in tallies)
    report["more"] = any(t.get("more") for t in tallies)
    logger.info("Storage purge (%s): %s", "dry run" if dry_run else "applied", report)
    return report
