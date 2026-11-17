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

"""The retention of a video's renders, and the one way a render is deleted.

Every render keeps its MP4 (tens of megabytes at 1080p60, more in 4K), its
poster, its captions, its frames and its inserts' crops, and a video is
rendered again after every round of edits, so renders are kept under a policy
rather than forever. The policy is a platform setting an administrator edits
(``PlatformSettings.video_render_retention``, :class:`Policy`), in days after a
render ended, 0 turning a rule off:

* ``failed_cancelled_days`` (1): a render that failed or was cancelled, a scene
  preview or a whole render;
* ``previews_days`` (7): a scene preview that ended ready;
* ``superseded_ready_days`` (30): a whole render that ended ready and is no
  longer the newest ready render of its quality.

What keeps a render (:func:`kept_by`, the one rule the sweep, ``get_video``,
the render list and ``render_cleanup`` read): the newest ready render of each
quality of its video (``latest_ready``); the video's current render - what its
cards, its chat card and its public link play - ``public_link`` while the video
has one, else ``latest_ready`` or, picked by hand, ``author``; a render a
manager kept (``keep``: ``author``); a render newer than its rule
(``retention``). A render in progress is never judged.

The nightly sweep (:func:`sweep_if_due`, from the autonomous cleanup loop: once
a night, install-wide) deletes every render nothing keeps (:func:`sweep`), one
video at a time under its row lock, and logs what it freed per video. A scene
preview that ends ready replaces the previous previews of its scene
(:func:`replaced_previews`, from the render job).

:func:`delete` is the one deletion of renders - the sweep's, a replaced
preview's, ``delete_video_renders`` and the Studio's cleanup
(``render_cleanup``), ``DELETE /renders/{id}``: in the caller's transaction the
rows and their files' rows (``assets.forget_files``: a file a video needs is
kept), then, once committed, :func:`purge` removes the stored objects and the
segments kept beside each render for a resume (``render_segments.forget``).
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    JOB_TERMINAL_STATUSES,
    VideoProject,
    VideoPublicLink,
    VideoRender,
)

logger = logging.getLogger(__name__)

#: The most days a rule keeps a render (about ten years): what Settings accepts.
MAX_DAYS = 3650
#: The videos one sweep looks at at most, so one tick of the cleanup loop never runs long.
SWEEP_VIDEOS = 100
#: The UTC hours the nightly sweep runs in; the cleanup loop asks every hour.
NIGHT_HOURS = range(0, 6)
#: Set once a night's sweep found nothing left past its rule, so the other hours and replicas skip it.
_SWEPT_KEY = "video_studio:render_retention:swept"
_SWEPT_SECONDS = 20 * 3600

#: Why a render is kept (:func:`kept_by`).
KEPT_LATEST_READY = "latest_ready"
KEPT_PUBLIC_LINK = "public_link"
KEPT_RETENTION = "retention"
KEPT_AUTHOR = "author"


def _now() -> datetime:
    return datetime.now(UTC)


# ── The policy ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    """How many days after it ended a render nothing else keeps is kept; 0 keeps it for good."""

    failed_cancelled_days: int = 1
    previews_days: int = 7
    superseded_ready_days: int = 30

    @classmethod
    def of(cls, stored: object) -> Policy:
        """The policy an administrator stored: a missing or unreadable value is its default, a stored one bounded."""
        if not isinstance(stored, Mapping):
            return cls()
        values: dict[str, int] = {}
        for rule in fields(cls):
            raw = stored.get(rule.name)
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                continue
            values[rule.name] = min(max(int(raw), 0), MAX_DAYS)
        return replace(cls(), **values)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def days_for(self, render: VideoRender) -> int:
        """The days of the rule *render* falls under (a render that ended)."""
        if render.status in (JOB_FAILED, JOB_CANCELLED):
            return self.failed_cancelled_days
        if render.scene_id is not None:
            return self.previews_days
        return self.superseded_ready_days


async def load_policy(db: AsyncSession) -> Policy:
    """The platform's render retention, its defaults when no administrator changed it."""
    from app.models.platform_settings import PlatformSettings

    stored = (await db.execute(select(PlatformSettings.video_render_retention).limit(1))).scalar_one_or_none()
    return Policy.of(stored)


async def save_policy(db: AsyncSession, values: Mapping[str, int]) -> tuple[Policy, Policy]:
    """Store the rules given in *values* (the others unchanged); ``(before, after)``. Does not commit."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    row = await get_platform_settings(db)
    before = Policy.of(row.video_render_retention)
    after = Policy.of({**before.as_dict(), **dict(values)})
    row.video_render_retention = after.as_dict()
    return before, after


# ── What keeps a render ───────────────────────────────────────────────────


def _newest_first(render: VideoRender) -> tuple[datetime, str]:
    return (render.created_at or datetime.min.replace(tzinfo=UTC), str(render.id))


def latest_ready_of(renders: Iterable[VideoRender]) -> dict[str, uuid.UUID]:
    """The newest ready whole render of each quality among *renders*: ``{quality: render id}``."""
    latest: dict[str, VideoRender] = {}
    for render in renders:
        if render.scene_id is not None or render.status != JOB_READY:
            continue
        best = latest.get(render.quality)
        if best is None or _newest_first(render) > _newest_first(best):
            latest[render.quality] = render
    return {quality: render.id for quality, render in latest.items()}


def newest_playable(renders: Iterable[VideoRender], *, excluding: Iterable[uuid.UUID] = ()) -> VideoRender | None:
    """The render a video plays once its current one is deleted: its newest other ready whole render with its MP4.

    The order of :func:`next_current`, for renders already read.
    """
    gone = set(excluding)
    playable = [
        render
        for render in renders
        if render.id not in gone and render.status == JOB_READY and render.scene_id is None and render.file_id is not None
    ]
    if not playable:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(playable, key=lambda r: (r.finished_at is not None, r.finished_at or floor, r.created_at or floor))


@dataclass(frozen=True)
class Standing:
    """What decides, for one video, which of its renders are kept (:func:`kept_by`)."""

    current_id: uuid.UUID | None
    public_link: bool
    #: ``{quality: id}`` of its newest ready whole render of each quality (:func:`latest_ready_of`).
    latest_ready: dict[str, uuid.UUID]
    policy: Policy
    now: datetime

    def is_latest_ready(self, render: VideoRender) -> bool:
        return self.latest_ready.get(render.quality) == render.id


def standing_of(
    project: VideoProject,
    renders: Iterable[VideoRender],
    *,
    public_link: bool,
    policy: Policy,
    now: datetime | None = None,
) -> Standing:
    """The standing of *project*, *renders* being every render it has."""
    return Standing(project.latest_render_id, public_link, latest_ready_of(renders), policy, now or _now())


async def has_public_link(db: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether the video has a public link: it plays the current render for anyone holding it, now or once the
    administrators turn public links back on."""
    found = await db.execute(select(VideoPublicLink.id).where(VideoPublicLink.project_id == project_id).limit(1))
    return found.first() is not None


async def standing(db: AsyncSession, project: VideoProject, *, renders: Iterable[VideoRender] | None = None) -> Standing:
    """The standing of *project* read from the database (its ready whole renders when *renders* is not given)."""
    if renders is None:
        renders = (
            (
                await db.execute(
                    select(VideoRender).where(
                        VideoRender.project_id == project.id,
                        VideoRender.scene_id.is_(None),
                        VideoRender.status == JOB_READY,
                    )
                )
            )
            .scalars()
            .all()
        )
    return standing_of(project, renders, public_link=await has_public_link(db, project.id), policy=await load_policy(db))


def expires_at(render: VideoRender, standing: Standing) -> datetime | None:
    """When the nightly sweep may delete *render*, or ``None`` when the retention never deletes it as things stand."""
    if render.status not in JOB_TERMINAL_STATUSES or render.keep:
        return None
    if render.id == standing.current_id or standing.is_latest_ready(render):
        return None
    days = standing.policy.days_for(render)
    ended = render.finished_at or render.created_at
    if days <= 0 or ended is None:
        return None
    return ended + timedelta(days=days)


def kept_by(render: VideoRender, standing: Standing) -> str | None:
    """Why the retention keeps *render* - ``latest_ready``, ``public_link``, ``author`` or ``retention`` - or ``None``:
    in progress (never judged), or past its rule, deleted by the next nightly sweep."""
    if render.status not in JOB_TERMINAL_STATUSES:
        return None
    if render.id == standing.current_id:
        if standing.public_link:
            return KEPT_PUBLIC_LINK
        return KEPT_LATEST_READY if standing.is_latest_ready(render) else KEPT_AUTHOR
    if standing.is_latest_ready(render):
        return KEPT_LATEST_READY
    if render.keep:
        return KEPT_AUTHOR
    due = expires_at(render, standing)
    if due is None or due > standing.now:
        return KEPT_RETENTION
    return None


# ── Deleting renders ──────────────────────────────────────────────────────


def render_files(render: VideoRender) -> list[uuid.UUID]:
    """The files a render owns: its MP4, its poster, its captions, its frames and its findings' frames and crops."""
    from app.core.utils import safe_uuid
    from app.services.video_studio.render_checks import frame_file_ids

    frames = [uuid.UUID(str(fid)) for fid in render.frame_file_ids or []]
    shown = [uid for uid in (safe_uuid(fid) for fid in frame_file_ids(render.checks)) if uid is not None]
    return [f for f in (render.file_id, render.poster_file_id, render.captions_file_id, *frames, *shown) if f]


async def _freed_sizes(db: AsyncSession, file_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """The bytes of each of *file_ids* deleting it frees: a stored file no video needs (``assets.forget_files``)."""
    from app.models.file_attachment import FileAttachment
    from app.services.video_studio import retention

    if not file_ids:
        return {}
    stored = dict((await db.execute(select(FileAttachment.id, FileAttachment.size).where(FileAttachment.id.in_(file_ids)))).all())
    needed = await retention.files_in_use(db, list(stored))
    return {fid: int(size or 0) for fid, size in stored.items() if str(fid) not in needed}


async def measure(db: AsyncSession, renders: Iterable[VideoRender]) -> dict[uuid.UUID, int]:
    """The bytes deleting each of *renders* frees: its stored files no video needs. ``{render id: bytes}``."""
    owned = {render.id: list(dict.fromkeys(render_files(render))) for render in renders}
    sizes = await _freed_sizes(db, list(dict.fromkeys(fid for files in owned.values() for fid in files)))
    return {render_id: sum(sizes.get(fid, 0) for fid in files) for render_id, files in owned.items()}


@dataclass
class Deletion:
    """Renders deleted in a transaction: what :func:`purge` removes once it committed."""

    render_ids: list[str] = field(default_factory=list)
    storage_keys: list[str] = field(default_factory=list)
    #: The bytes of the files deleted.
    bytes: int = 0


async def delete(db: AsyncSession, renders: Iterable[VideoRender]) -> Deletion:
    """Delete *renders* and their files' rows in *db*'s transaction (not committed); then :func:`purge`.

    A video that lost a ready render is indexed again when it is written: the media that render drew are no longer
    needed for it (``retention.media_index``).
    """
    from app.services.video_studio import assets, retention

    rows = list(renders)
    if not rows:
        return Deletion()
    file_ids = list(dict.fromkeys(fid for render in rows for fid in render_files(render)))
    freed = await _freed_sizes(db, file_ids)
    keys = await assets.forget_files(db, file_ids)
    drew = {render.project_id for render in rows if render.status == JOB_READY and render.asset_refs}
    for render in rows:
        await db.delete(render)
    if drew:
        await db.flush()
        for project_id in drew:
            if (project := await db.get(VideoProject, project_id)) is not None:
                retention.mark_for_reindex(project)
    return Deletion([str(render.id) for render in rows], keys, sum(freed.values()))


async def purge(deletion: Deletion) -> None:
    """Remove what a committed :func:`delete` left in the store: the files' objects, the segments kept for a resume."""
    from app.services.file_cleanup import purge_storage_objects
    from app.services.video_studio import render_segments

    if deletion.storage_keys:
        await purge_storage_objects(deletion.storage_keys)
    for render_id in deletion.render_ids:
        try:
            await render_segments.forget(render_id)
        except Exception as exc:  # the hourly segment sweep removes those of a render that is gone
            logger.info("Video render %s: its kept segments were not deleted: %s", render_id, exc)


async def replaced_previews(db: AsyncSession, render: VideoRender) -> Deletion:
    """A scene preview that ended ready replaces the previous previews of its scene: delete them (not committed).

    The frames they drew may stay in the figure and insert caches, which evict on their own.
    """
    if render.scene_id is None or render.status != JOB_READY:
        return Deletion()
    previous = await db.execute(
        select(VideoRender)
        .where(
            VideoRender.project_id == render.project_id,
            VideoRender.scene_id == render.scene_id,
            VideoRender.id != render.id,
            VideoRender.status.in_(tuple(JOB_TERMINAL_STATUSES)),
            VideoRender.keep.is_(False),
            VideoRender.created_at < render.created_at,
        )
        .with_for_update(skip_locked=True)
    )
    return await delete(db, previous.scalars().all())


async def lock_render(db: AsyncSession, render: VideoRender) -> bool:
    """Lock *render* before changing it (made current, deleted by hand): ``False`` when it was deleted meanwhile.

    Taken before its video's row, the order the render job takes them in; a sweep or a cleanup holding the video
    skips a render locked here, and reads one whose change committed as it is now.
    """
    found = await db.execute(select(VideoRender.id).where(VideoRender.id == render.id).with_for_update())
    if found.scalar_one_or_none() is None:
        return False
    await db.refresh(render)
    return True


async def next_current(db: AsyncSession, project: VideoProject, *, excluding: uuid.UUID) -> uuid.UUID | None:
    """The render a video plays once its current one is deleted: its newest other ready render (:func:`newest_playable`)."""
    found = await db.execute(
        select(VideoRender.id)
        .where(
            and_(
                VideoRender.project_id == project.id,
                VideoRender.status == JOB_READY,
                VideoRender.id != excluding,
                VideoRender.file_id.is_not(None),
                VideoRender.scene_id.is_(None),
            )
        )
        .order_by(VideoRender.finished_at.desc().nulls_last(), VideoRender.created_at.desc())
        .limit(1)
    )
    return found.scalar_one_or_none()


# ── The nightly sweep ─────────────────────────────────────────────────────


def _due_videos(policy: Policy, now: datetime) -> Select | None:
    """The videos with a render past its rule, oldest first: what :func:`sweep` looks at. ``None`` with every rule off.

    A coarse pass in SQL - the newest ready render of each quality, the current one and the kept ones left out -
    that :func:`kept_by` then judges render by render.
    """
    ended = func.coalesce(VideoRender.finished_at, VideoRender.created_at)
    ranked = select(
        VideoRender.id.label("render_id"),
        func.row_number()
        .over(
            partition_by=(VideoRender.project_id, VideoRender.quality, VideoRender.scene_id.is_(None), VideoRender.status == JOB_READY),
            order_by=(VideoRender.created_at.desc(), VideoRender.id.desc()),
        )
        .label("rank"),
    ).subquery()
    rules = []
    if policy.failed_cancelled_days > 0:
        rules.append(
            and_(
                VideoRender.status.in_((JOB_FAILED, JOB_CANCELLED)),
                ended < now - timedelta(days=policy.failed_cancelled_days),
            )
        )
    if policy.previews_days > 0:
        rules.append(
            and_(
                VideoRender.status == JOB_READY,
                VideoRender.scene_id.is_not(None),
                ended < now - timedelta(days=policy.previews_days),
            )
        )
    if policy.superseded_ready_days > 0:
        rules.append(
            and_(
                VideoRender.status == JOB_READY,
                VideoRender.scene_id.is_(None),
                ranked.c.rank > 1,
                ended < now - timedelta(days=policy.superseded_ready_days),
            )
        )
    if not rules:
        return None
    return (
        select(VideoRender.project_id)
        .join(ranked, ranked.c.render_id == VideoRender.id)
        .join(VideoProject, VideoProject.id == VideoRender.project_id)
        .where(
            VideoRender.keep.is_(False),
            or_(VideoProject.latest_render_id.is_(None), VideoProject.latest_render_id != VideoRender.id),
            or_(*rules),
        )
        .group_by(VideoRender.project_id)
        .order_by(func.min(VideoRender.created_at))
    )


@dataclass(frozen=True)
class Freed:
    """What one video's sweep deleted."""

    project_id: uuid.UUID
    title: str
    renders: int
    previews: int
    failed_cancelled: int
    bytes: int

    def sentence(self) -> str:
        parts = [
            f"{count} {label}"
            for count, label in (
                (self.failed_cancelled, "failed or cancelled"),
                (self.previews, "scene previews"),
                (self.renders - self.previews - self.failed_cancelled, "superseded"),
            )
            if count
        ]
        return f"video {self.project_id} ({self.title!r}): {self.renders} renders ({', '.join(parts)}), {self.bytes} bytes freed"


@dataclass(frozen=True)
class Swept:
    freed: list[Freed]
    #: Every video with a render past its rule was looked at: none left for tonight.
    complete: bool

    @property
    def renders(self) -> int:
        return sum(video.renders for video in self.freed)


async def _sweep_video(db: AsyncSession, project_id: uuid.UUID, policy: Policy, now: datetime) -> Freed | None:
    """Delete the renders of one video nothing keeps, under the video's row lock; commits. ``None`` for none (or a
    video another sweep or cleanup holds: skipped, never waited on).

    Ends its transaction with a commit even when it wrote nothing: a rollback would expire what the cleanup loop's
    session holds.
    """
    fresh = {"populate_existing": True}
    project = (
        await db.execute(
            select(VideoProject).where(VideoProject.id == project_id).with_for_update(skip_locked=True).execution_options(**fresh)
        )
    ).scalar_one_or_none()
    if project is None:
        await db.commit()
        return None
    renders = list(
        (await db.execute(select(VideoRender).where(VideoRender.project_id == project_id).execution_options(**fresh))).scalars().all()
    )
    held = standing_of(project, renders, public_link=await has_public_link(db, project_id), policy=policy, now=now)
    due = [render.id for render in renders if render.status in JOB_TERMINAL_STATUSES and kept_by(render, held) is None]
    rows = []
    if due:
        # A render someone is changing (kept, made current) is skipped; one changed since it was read is judged again.
        locked = await db.execute(
            select(VideoRender).where(VideoRender.id.in_(due)).with_for_update(skip_locked=True).execution_options(**fresh)
        )
        rows = [render for render in locked.scalars().all() if kept_by(render, held) is None]
    if not rows:
        await db.commit()
        return None
    title = project.title
    deletion = await delete(db, rows)
    await db.commit()
    await purge(deletion)
    return Freed(
        project_id=project_id,
        title=title,
        renders=len(rows),
        previews=sum(1 for r in rows if r.scene_id is not None and r.status == JOB_READY),
        failed_cancelled=sum(1 for r in rows if r.status in (JOB_FAILED, JOB_CANCELLED)),
        bytes=deletion.bytes,
    )


async def sweep(db: AsyncSession, *, now: datetime | None = None, videos: int = SWEEP_VIDEOS) -> Swept:
    """Delete the renders nothing keeps (:func:`kept_by`), video by video, at most *videos* of them. Commits each video."""
    policy = await load_policy(db)
    now = now or _now()
    query = _due_videos(policy, now)
    if query is None:
        return Swept([], complete=True)
    project_ids = list((await db.execute(query.limit(videos + 1))).scalars().all())
    await db.commit()
    freed: list[Freed] = []
    for project_id in project_ids[:videos]:
        video = await _sweep_video(db, project_id, policy, now)
        if video is not None:
            logger.info("Video Studio render retention: %s", video.sentence())
            freed.append(video)
    return Swept(freed, complete=len(project_ids) <= videos)


async def sweep_if_due(db: AsyncSession, *, now: datetime | None = None) -> Swept | None:
    """Run :func:`sweep` during the night (:data:`NIGHT_HOURS`, UTC) until one found nothing left; ``None`` when not due.

    The cleanup loop asks every hour on every replica: the first sweep of the night that looks at every video due
    marks the night swept in Redis, and the others skip it. Two replicas sweeping at once split the videos (each
    skips a video the other holds). Redis unreachable: the sweep runs, finding little the second time.
    """
    from app.core.redis import redis_client

    now = now or _now()
    if now.hour not in NIGHT_HOURS:
        return None
    redis = None
    try:
        redis = await redis_client()
        if await redis.exists(_SWEPT_KEY):
            return None
    except Exception as exc:
        logger.debug("Video Studio render retention: whether tonight was swept was not read: %s", exc)
        redis = None
    swept = await sweep(db, now=now)
    if swept.complete and redis is not None:
        with contextlib.suppress(Exception):
            await redis.set(_SWEPT_KEY, "1", ex=_SWEPT_SECONDS)
    return swept
