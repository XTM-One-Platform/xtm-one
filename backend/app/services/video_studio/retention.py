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

"""The retention of recordings' and AI shots' media.

A recording is a mezzanine video of the backend browser and an AI shot a
provider's clip: the heavy files of the Video Studio. The retention cleans up
the ones no video uses: a recording or a shot a video uses is never
expired, however long the video is left alone, so the video can always be
edited and rendered again. The policy, in days (``config``):
``video_studio_media_retention_days`` after the last video using them stopped
(:func:`touch` stamps ``last_used_at``: a save naming them or dropping them, a
render drawing them, the video deleted),
``video_studio_unused_media_retention_days`` after they were made when no video
ever did. ``keep`` exempts one; 0 keeps them forever.

The hourly sweep (:func:`sweep`, from the autonomous cleanup loop) deletes the
media files and marks the row ``expired_at``: the row stays with its name,
poster, script and prompt, so the person sees what expired and records or
generates it again. The stamp only says when to look: before deleting, the
sweep checks that no video's spec names the media, and stamps one a video
uses now instead (in a video left alone past the period, made before the
retention existed, or named by a spec written another way).

A recording or a shot that failed or was cancelled made nothing worth keeping:
once no video uses it and :data:`FAILED_MEDIA_GRACE` has passed (the assistant
may still generate it again in its turn), the sweep deletes it, row included.
One a video uses stays while it does, shown failed in its scene with
a way to generate it again.

Which videos use a medium is read from ``video_projects.asset_refs`` (GIN-indexed):
the one reference index (:func:`media_index`) of every
medium a video needs - the recordings, AI shots, AI tracks and files its spec
names, wherever it names them (a screen insert's source, a backdrop, an image
scene, an overlay, a figure's pictures, the logo, the music), what the shots it
plays are made from (``VideoClip.source_refs``: a start frame, the recording or
shot it was cut from, reference images, ingredients) and theirs in turn, and
what its ready renders drew (``VideoRender.asset_refs``: a revision the spec
moved on from). Every write of a video indexes it (the model's mapper event);
a shot whose sources change indexes the videos naming it again
(:func:`reindex_users_of`); a video indexed by an older rule is indexed again
by the sweep (:func:`index_stale_refs`). :func:`uses_media` is the one
predicate, for every video whoever owns it (the sweep, ``in_use``, the files a
deletion would remove - :func:`files_in_use`) or narrowed to what a reader may
open (``library.used_in``, ``projects.can_read_project_asset``);
:func:`named_by_a_video` is its form for each row of a query (the library's
``usage`` filter). No cap, no scan of the specs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, literal, null, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    JOB_TERMINAL_STATUSES,
    VideoClip,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.video_studio.spec import ASSET_REFS_VERSION, asset_ref, asset_refs, referenced_ids

logger = logging.getLogger(__name__)

#: Rows one sweep looks at per kind, so one tick never holds a long transaction.
SWEEP_BATCH = 100

#: How long a failed or cancelled recording or shot no video uses is kept: time
#: for the assistant to generate it again in the turn that asked for it.
FAILED_MEDIA_GRACE = timedelta(hours=1)
#: How many shots deep the media a shot is made from are followed: a shot started from a frame of a shot started
#: from a recording, and so on. Past it the chain is not followed further.
MAX_SOURCE_DEPTH = 8
#: The batches of videos indexed by an older rule one sweep indexes again (:func:`index_stale_refs`).
STALE_BATCHES = 50


def _now() -> datetime:
    return datetime.now(UTC)


async def touch(db: AsyncSession, *specs: dict) -> None:
    """Stamp the recordings and AI shots the *specs* name as used now (not committed).

    A save passes the spec it replaces with the one it stores (what it drops was
    used until now), a deletion the video's last spec: the retention of media
    no video uses counts from then.
    """
    takes: set[str] = set()
    clips: set[str] = set()
    for spec in specs:
        refs = referenced_ids(spec or {})
        takes |= refs["take"]
        clips |= refs["clip"]
    now = _now()
    for model, ids in ((VideoTake, takes), (VideoClip, clips)):
        keys = [uuid.UUID(value) for value in sorted(ids)]
        if keys:
            await db.execute(update(model).where(model.id.in_(keys)).values(last_used_at=now))


def expires_at(row: VideoTake | VideoClip, *, in_use: bool = False) -> datetime | None:
    """When the sweep will delete *row*'s media, or ``None`` when it will not.

    *in_use*: a video uses it, so it is never expired while it does.
    """
    if in_use or row.keep or row.expired_at is not None or row.status != JOB_READY:
        return None
    if row.last_used_at is not None:
        days, since = settings.video_studio_media_retention_days, row.last_used_at
    else:
        days, since = settings.video_studio_unused_media_retention_days, row.created_at
    if days <= 0 or since is None:
        return None
    return since + timedelta(days=days)


def retention_fields(row: VideoTake | VideoClip, *, in_use: bool = False) -> dict:
    """What the library shows of a row's retention; *in_use*: a video uses it."""
    due = expires_at(row, in_use=in_use)
    return {
        "keep": bool(row.keep),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expired_at": row.expired_at.isoformat() if row.expired_at else None,
        "expires_at": due.isoformat() if due else None,
    }


def _owned_files(row: VideoTake | VideoClip) -> list:
    """The heavy files the expiry deletes: a shot's video, and the videos its params keep beside it."""
    if isinstance(row, VideoTake):
        from app.services.video_studio.take_repair import keyframes_file
        from app.services.video_studio.take_window import frame_list_file

        # Its keyframes and frame list expire with its video: a take expired is never repaired.
        return [row.file_id, row.contact_sheet_file_id, keyframes_file(row.capture), frame_list_file(row.capture)]
    from app.services.video_studio import clips

    return [row.file_id, *clips.side_files(row.params, videos_only=True)]


def media_keys(media: Iterable[tuple[str, object]]) -> list[str]:
    """The ``asset_refs`` keys of ``(kind, id)`` pairs, deduplicated; a pair with no valid id is dropped."""
    return sorted({key for key in (asset_ref(kind, asset_id) for kind, asset_id in media) if key})


def _indexed_refs():
    """SQL: ``asset_refs::jsonb``, the expression ``ix_video_projects_asset_refs_jsonb_gin`` indexes."""
    return cast(VideoProject.asset_refs, JSONB)


def uses_media(keys: list[str]):
    """SQL: the video names one of *keys* (``media_keys``) - ``jsonb ?| text[]``, served by the GIN index."""
    return _indexed_refs().op("?|")(cast(keys, ARRAY(Text)))


def named_by_a_video(model: type[VideoTake] | type[VideoClip], kind: str):
    """SQL: some video names the *model* row of the enclosing query - :func:`uses_media` row by row, every video.

    ``EXISTS`` over ``asset_refs ? '<kind>:<id>'`` (the key :func:`media_keys` builds), served by the same GIN index.
    """
    # Grouped: ``?`` and ``||`` share a precedence, and ``refs ? 'clip:' || id`` reads as ``(refs ? 'clip:') || id``.
    key = (literal(f"{kind}:", Text) + cast(model.id, Text)).self_group()
    return select(VideoProject.id).where(_indexed_refs().op("?")(key)).exists()


#: One step of :func:`_indexing`: the statement whose rows it needs next.
_Indexing = Generator[Any, list, list[str]]


def _indexing(spec: dict, project_id: object) -> _Indexing:
    """The reference index of one video (:func:`media_index`), as the statements it reads: send back their rows.

    One rule for the mapper event, which reads on the flush's connection
    (:func:`media_index`), and for the sweep, which reads on a session
    (:func:`media_index_async`).
    """
    from app.services.video_studio.clips import source_refs

    keys = set(asset_refs(spec))
    if project_id is not None:
        drawn = yield select(VideoRender.asset_refs).where(
            VideoRender.project_id == project_id,
            VideoRender.status == JOB_READY,
            VideoRender.asset_refs.is_not(None),
        )
        for (refs,) in drawn:
            keys.update(key for key in refs or [] if isinstance(key, str))
    walked: set[str] = set()
    shots = {key for key in keys if key.startswith("clip:")}
    for _depth in range(MAX_SOURCE_DEPTH):
        todo = sorted(shots - walked)
        if not todo:
            break
        walked.update(todo)
        # A shot written before its sources were stored is read from its params, once.
        rows = yield select(
            VideoClip.source_refs, case((VideoClip.source_refs.is_(None), VideoClip.params), else_=null())
        ).where(VideoClip.id.in_([uuid.UUID(key.split(":", 1)[1]) for key in todo]))
        found: set[str] = set()
        for refs, params in rows:
            found.update(refs if refs is not None else source_refs(params if isinstance(params, dict) else {}))
        shots = {key for key in found - keys if key.startswith("clip:")}
        keys |= found
    return sorted(keys)


def media_index(connection: Any, spec: dict, project_id: object = None) -> list[str]:
    """Every medium the video *project_id* needs, as ``asset_refs`` keys, read on a sync *connection*.

    What its *spec* names (``spec.asset_refs``), what its ready renders drew
    and, shot by shot, what the AI shots among them are made from
    (:data:`MAX_SOURCE_DEPTH` deep). The model's mapper event writes it on
    every flush of the video.
    """
    steps = _indexing(spec, project_id)
    try:
        statement = next(steps)
        while True:
            statement = steps.send(list(connection.execute(statement).all()))
    except StopIteration as done:
        return done.value


async def media_index_async(db: AsyncSession, spec: dict, project_id: object = None) -> list[str]:
    """:func:`media_index` read on a session."""
    steps = _indexing(spec, project_id)
    try:
        statement = next(steps)
        while True:
            statement = steps.send(list((await db.execute(statement)).all()))
    except StopIteration as done:
        return done.value


def reindex_users_of(connection: Any, key: str) -> int:
    """Index again, on a sync *connection*, the videos that need the medium *key*; how many.

    A shot generated again from another start, with other references: the
    videos that play it - directly or through a shot made from it - now need
    those. Their ``updated_at`` is kept: indexing a video is not an edit.
    """
    table = VideoProject.__table__
    rows = connection.execute(select(VideoProject.id, VideoProject.spec).where(uses_media([key]))).all()
    for project_id, spec in rows:
        connection.execute(
            update(table)
            .where(table.c.id == project_id)
            .values(
                asset_refs=media_index(connection, dict(spec or {}), project_id),
                asset_refs_version=ASSET_REFS_VERSION,
                updated_at=table.c.updated_at,
            )
        )
    return len(rows)


def mark_for_reindex(project: VideoProject) -> None:
    """Have the next flush of *project* index it again: a render of it became ready (what it drew is needed)."""
    from sqlalchemy.exc import InvalidRequestError
    from sqlalchemy.orm.attributes import flag_modified

    try:
        flag_modified(project, "asset_refs")
    except InvalidRequestError:
        # Not loaded: written, the flush indexes it all the same (``models.video_studio._index_asset_refs``).
        project.asset_refs = []


async def files_in_use(db: AsyncSession, file_ids: Iterable[object]) -> set[str]:
    """The ids of *file_ids* any video needs (:func:`media_index`): a deletion keeps them."""
    return await media_in_use(db, [("file", file_id) for file_id in file_ids])


async def media_in_use(db: AsyncSession, media: Iterable[tuple[str, object]]) -> set[str]:
    """The ids of *media* (``(kind, id)`` pairs) that any video uses, the person's or not."""
    keys = media_keys(media)
    if not keys:
        return set()
    wanted = set(keys)
    found: set[str] = set()
    for refs in (await db.execute(select(VideoProject.asset_refs).where(uses_media(keys)))).scalars().all():
        found |= {key.split(":", 1)[1] for key in refs or [] if key in wanted}
    return found


async def _referenced(db: AsyncSession, asset_id: uuid.UUID, kind: str) -> bool:
    """Whether any video's spec names the asset."""
    keys = media_keys([(kind, asset_id)])
    if not keys:
        return False
    rows = await db.execute(select(VideoProject.id).where(uses_media(keys)).limit(1))
    return rows.scalars().first() is not None


async def repaired_now(db: AsyncSession) -> set[str]:
    """The ids of the recordings a repair queued or running is made from (``take_repair.repair_source``): the
    sweep never expires one while its repair needs its MP4 or its keyframes."""
    from app.services.video_studio.take_repair import repair_source

    rows = await db.execute(
        select(VideoTake.capture).where(VideoTake.status.not_in(JOB_TERMINAL_STATUSES), VideoTake.capture.is_not(None))
    )
    return {source for capture in rows.scalars().all() if (source := repair_source(capture))}


async def in_use(db: AsyncSession, row: VideoTake | VideoClip) -> bool:
    """Whether any video uses *row*, the person's or not."""
    return await _referenced(db, row.id, "take" if isinstance(row, VideoTake) else "clip")


def _stale():
    """SQL: a video not indexed (an older replica wrote it), or indexed by an older rule (:data:`ASSET_REFS_VERSION`)."""
    return or_(
        VideoProject.asset_refs.is_(None),
        VideoProject.asset_refs_version.is_(None),
        VideoProject.asset_refs_version < ASSET_REFS_VERSION,
    )


async def index_stale_refs(db: AsyncSession, batch: int = SWEEP_BATCH, *, batches: int = STALE_BATCHES) -> int:
    """Index again the videos an older replica or an older rule left (:func:`_stale`); how many. Commits each batch.

    Until then a video keeps the index it had, which already holds the media
    its scenes play: nothing it names is ever read as unused meanwhile. A
    video's ``updated_at`` is kept: indexing it is not an edit.
    """
    indexed = 0
    for _ in range(max(1, batches)):
        rows = (await db.execute(select(VideoProject.id, VideoProject.spec).where(_stale()).limit(batch))).all()
        for project_id, spec in rows:
            await db.execute(
                update(VideoProject)
                .where(VideoProject.id == project_id)
                .values(
                    asset_refs=await media_index_async(db, dict(spec or {}), project_id),
                    asset_refs_version=ASSET_REFS_VERSION,
                    updated_at=VideoProject.updated_at,
                )
            )
        indexed += len(rows)
        if len(rows) < batch:
            break
        await db.commit()
    return indexed


async def _purge_failed(db: AsyncSession, now: datetime, batch: int) -> tuple[int, list]:
    """Delete the failed and cancelled recordings and shots no video uses, past the grace: ``(count, files)``."""
    purged, files = 0, []
    for model, kind in ((VideoTake, "take"), (VideoClip, "clip")):
        rows = (
            (
                await db.execute(
                    select(model)
                    .where(
                        model.status.in_((JOB_FAILED, JOB_CANCELLED)),
                        func.coalesce(model.finished_at, model.created_at) < now - FAILED_MEDIA_GRACE,
                    )
                    .order_by(model.created_at)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if await _referenced(db, row.id, kind):
                continue
            # The poster and a shot's kept frames outlive its media's expiry, not the row.
            if isinstance(row, VideoClip):
                from app.services.video_studio import clips

                files.extend(clips.owned_files(row))
            else:
                files.extend([*_owned_files(row), row.poster_file_id])
            await db.delete(row)
            purged += 1
    return purged, files


async def sweep(db: AsyncSession, *, now: datetime | None = None, batch: int = SWEEP_BATCH) -> dict[str, int]:
    """Purge failed media, expire the media past retention; ``{"purged", "expired", "in_use"}``. Commits.

    ``in_use`` counts the media past their period that a video still uses:
    stamped now, never expired.
    """
    from app.services.video_studio import assets

    used_days = settings.video_studio_media_retention_days
    unused_days = settings.video_studio_unused_media_retention_days
    now = now or _now()
    await index_stale_refs(db, batch)
    purged, files = await _purge_failed(db, now, batch)
    counts = {"purged": purged, "expired": 0, "in_use": 0}
    if used_days <= 0 and unused_days <= 0:
        await db.commit()
        if files:
            await assets.delete_files(files)
        return counts
    repairing = await repaired_now(db)
    for model, kind in ((VideoTake, "take"), (VideoClip, "clip")):
        due = []
        if used_days > 0:
            due.append(and_(model.last_used_at.is_not(None), model.last_used_at < now - timedelta(days=used_days)))
        if unused_days > 0:
            due.append(and_(model.last_used_at.is_(None), model.created_at < now - timedelta(days=unused_days)))
        rows = (
            (
                await db.execute(
                    select(model)
                    .where(
                        model.status == JOB_READY,
                        model.keep.is_(False),
                        model.expired_at.is_(None),
                        or_(*due),
                    )
                    .order_by(model.created_at)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            # A recording a repair is made from is in use until that repair ends.
            if (kind == "take" and str(row.id) in repairing) or await _referenced(db, row.id, kind):
                row.last_used_at = now
                counts["in_use"] += 1
                continue
            files.extend(_owned_files(row))
            row.file_id = None
            if isinstance(row, VideoTake):
                row.contact_sheet_file_id = None
                if isinstance(row.capture, dict) and "keyframes" in row.capture:
                    row.capture = {k: v for k, v in row.capture.items() if k != "keyframes"}
            row.expired_at = now
            counts["expired"] += 1
    await db.commit()
    if files:
        await assets.delete_files(files)
    return counts
