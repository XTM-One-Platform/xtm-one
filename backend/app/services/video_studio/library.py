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

"""The Video Studio's libraries, a page at a time.

AI generation fills them fast - dozens of AI shots a video - so no list is
read whole: the videos and the media are filtered, searched and paginated in
SQL, and a page carries the total of what matched, so every row stays
reachable whatever the library's size.

* :func:`project_filters` - the videos the caller can open, narrowed to
  theirs, shared with them or the company's, searched by title, template or
  owner name: the rule every library page shares with the presentations
  (``services/library_filters``).
* :func:`asset_page` - the person's recordings and AI shots together, newest
  first, by kind, searched by name or prompt, by use (some video names it,
  anyone's, or none does), by when they were made, or by id (the video's own,
  whatever their age). A failed or cancelled one made nothing and is never
  listed as media: its scene reads it by id.
* :func:`used_in` - the videos the reader can open that use the media of a
  page, read for that page only from the index of what each video names
  (``retention.uses_media``): id and title, never the specs.
* :func:`deletion_refusal` - why a medium a video uses is not deleted unless
  its deletion is forced: the videos the reader can open, how many
  others.
* :func:`remove_media` / :func:`delete_assets` - a medium deleted from the
  library, alone or in bulk: the same files either way. In bulk,
  exactly what the library lists with the same filters, and never a medium
  a video uses: those are kept and counted - forcing one is a deletion of its
  own, with ``deletion_refusal``.

A search matches when every word is found in one of the fields, any case.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, VideoClip, VideoProject, VideoTake
from app.services.library_filters import LibraryVisibility, library_filters, search_words
from app.services.video_studio import retention

VideoVisibility = LibraryVisibility
AssetKind = Literal["all", "take", "clip"]
#: ``used``: some video names it, whoever owns the video; ``unused``: none does - what the retention reads.
AssetUsage = Literal["all", "used", "unused"]
#: The orders of the media library (``GET /video-studio/assets?sort=``): when made, or by title.
AssetSort = Literal["created_at", "title"]
SortDirection = Literal["asc", "desc"]

#: The most rows one page may hold.
MAX_PAGE_SIZE = 100
#: A page of the Videos tab, and of the Media tab, unless the caller asks another size.
PROJECT_PAGE_SIZE = 24
ASSET_PAGE_SIZE = 48
#: The media a bulk deletion removes per transaction: the rows it holds and the files it forgets stay bounded.
DELETE_BATCH = 100

_MODELS: dict[str, type[VideoTake] | type[VideoClip]] = {"take": VideoTake, "clip": VideoClip}


def project_filters(
    user: User, groups: list, *, search: str | None = None, visibility: VideoVisibility = "all"
) -> list:
    """The SQL predicates of the videos *user* can open that match *search* and *visibility*."""
    return library_filters(VideoProject, user, groups, search=search, visibility=visibility)


@dataclass(frozen=True)
class AssetFilters:
    """What the media library is narrowed to: the filters of a page, and of a bulk deletion by filters.

    *ids* keeps only those (``None``: any), *exclude* leaves those out,
    *available* leaves out the ones whose media expired, *usage* reads whether
    any video names them (``retention.named_by_a_video``), *created_from* /
    *created_to* bound when they were made, both included (an instant without
    a zone is read as UTC).
    """

    kind: AssetKind = "all"
    search: str | None = None
    ids: list[uuid.UUID] | None = None
    exclude: list[uuid.UUID] = field(default_factory=list)
    available: bool = False
    usage: AssetUsage = "all"
    created_from: datetime | None = None
    created_to: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("created_from", "created_to"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                object.__setattr__(self, name, value.replace(tzinfo=UTC))


def _asset_filters(model: type[VideoTake] | type[VideoClip], label: str, user: User, filters: AssetFilters) -> list:
    where = [model.user_id == user.id, model.status.not_in((JOB_FAILED, JOB_CANCELLED))]
    if filters.available:
        where.append(model.expired_at.is_(None))
    fields = [model.name] if model is VideoTake else [model.name, model.prompt]
    for word in search_words(filters.search):
        where.append(or_(*(field.icontains(word, autoescape=True) for field in fields)))
    if filters.ids is not None:
        where.append(model.id.in_(filters.ids))
    if filters.exclude:
        where.append(model.id.not_in(filters.exclude))
    if filters.usage != "all":
        used = retention.named_by_a_video(model, label)
        where.append(used if filters.usage == "used" else ~used)
    if filters.created_from is not None:
        where.append(model.created_at >= filters.created_from)
    if filters.created_to is not None:
        where.append(model.created_at <= filters.created_to)
    return where


def _title(model: type[VideoTake] | type[VideoClip]):
    """The title the library shows: a recording's name, an AI shot's name else its prompt (lower case, for the order)."""
    shown = [model.name] if model is VideoTake else [model.name, model.prompt]
    return func.lower(func.coalesce(*shown, ""))


def _listing(user: User, filters: AssetFilters):
    """The ``(id, kind, created_at, title)`` of every medium *filters* keep, both kinds in one ``UNION ALL``."""
    parts = [
        select(
            model.id.label("id"),
            literal(label).label("kind"),
            model.created_at.label("created_at"),
            _title(model).label("title"),
        ).where(*_asset_filters(model, label, user, filters))
        for label, model in _MODELS.items()
        if filters.kind in ("all", label)
    ]
    return (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()


async def _ranked(
    db: AsyncSession,
    listing,
    *,
    limit: int,
    offset: int = 0,
    sort: AssetSort = "created_at",
    direction: SortDirection = "desc",
) -> list[tuple[str, VideoTake | VideoClip]]:
    """The rows of *listing* in the order asked (newest first by default), from *offset*: ranked once, then loaded by id.

    The newest medium, then the id, break the ties, so a page never repeats or skips a row.
    """
    column = listing.c.title if sort == "title" else listing.c.created_at
    order = [column.asc() if direction == "asc" else column.desc()]
    if sort != "created_at":
        order.append(listing.c.created_at.desc())
    order.append(listing.c.id.desc())
    keys = (await db.execute(select(listing.c.id, listing.c.kind).order_by(*order).offset(offset).limit(limit))).all()
    loaded: dict[tuple[str, uuid.UUID], VideoTake | VideoClip] = {}
    for label, model in _MODELS.items():
        wanted = [key.id for key in keys if key.kind == label]
        if wanted:
            for row in (await db.execute(select(model).where(model.id.in_(wanted)))).scalars().all():
                loaded[(label, row.id)] = row
    return [(key.kind, loaded[(key.kind, key.id)]) for key in keys if (key.kind, key.id) in loaded]


async def asset_page(
    db: AsyncSession,
    user: User,
    filters: AssetFilters | None = None,
    *,
    limit: int = ASSET_PAGE_SIZE,
    offset: int = 0,
    sort: AssetSort = "created_at",
    direction: SortDirection = "desc",
) -> tuple[list[tuple[str, VideoTake | VideoClip]], int]:
    """``([(kind, row)], total)``: one page of *user*'s recordings and AI shots that *filters* keep, newest first by default.

    The two kinds are ranked in one query (a ``UNION ALL`` of ids, dates and
    titles) by *sort* and *direction*, then the page's rows are loaded by id.
    """
    listing = _listing(user, filters or AssetFilters())
    total = (await db.execute(select(func.count()).select_from(listing))).scalar_one()
    return await _ranked(db, listing, limit=limit, offset=offset, sort=sort, direction=direction), total


def owned_files(kind: str, row: VideoTake | VideoClip) -> list:
    """Every file a recording or an AI shot owns: what deleting it deletes."""
    if kind == "take":
        from app.services.video_studio.take_repair import keyframes_file
        from app.services.video_studio.take_window import frame_list_file

        # Its keyframes too: what a repair of it was made from; and its frame list.
        return [
            row.file_id,
            row.poster_file_id,
            row.contact_sheet_file_id,
            keyframes_file(row.capture),
            frame_list_file(row.capture),
        ]
    from app.services.video_studio import clips

    # Its media, poster and frames, and what it keeps beside them: a take replaced, the attempts.
    return clips.owned_files(row)


async def remove_media(db: AsyncSession, media: list[tuple[str, VideoTake | VideoClip]]) -> list:
    """Cancel and delete *media* (``(kind, row)``) in *db*'s transaction (not committed): the files they owned."""
    from app.services.video_studio import jobs

    files: list = []
    for kind, row in media:
        files.extend(owned_files(kind, row))
        await jobs.cancel(db, kind, row)
        await db.delete(row)
    return files


@dataclass
class DeletedMedia:
    """What a bulk deletion removed: how many of each kind, the storage objects left to purge, and what it kept.

    *kept*: the media named or matched that a video used, which a bulk deletion never deletes.
    """

    takes: int = 0
    clips: int = 0
    kept: int = 0
    storage_keys: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.takes + self.clips


async def _delete_batch(db: AsyncSession, media: list[tuple[str, VideoTake | VideoClip]], done: DeletedMedia) -> None:
    from app.services.video_studio import assets

    files = await remove_media(db, media)
    done.storage_keys.extend(await assets.forget_files(db, files))
    await db.commit()
    done.takes += sum(1 for kind, _row in media if kind == "take")
    done.clips += sum(1 for kind, _row in media if kind == "clip")


async def delete_assets(
    db: AsyncSession,
    user: User,
    *,
    items: list[tuple[str, uuid.UUID]] | None = None,
    filters: AssetFilters | None = None,
) -> DeletedMedia:
    """Delete *user*'s media named by *items* (``(kind, id)``), or every one *filters* keep. Commits.

    Only what the library lists for *user* is ever deleted: their own
    recordings and shots, an id naming anything else skipped like an unknown
    one. A medium any video uses - anyone's - is never deleted in bulk: it is
    kept and counted (``kept``), as a single deletion refuses it unless forced.
    Each batch of :data:`DELETE_BATCH` is its own transaction, its
    rows and their file rows deleted together, the use read again as the
    batch is read: a video saved before a batch is read keeps its media. The
    read and the delete are no single atomic step - a video's save
    writes its ``asset_refs`` and locks no medium -, so one committed while
    its batch deletes is not seen, and then names a medium that is gone. The
    storage objects are returned, for the caller to purge once it answered.
    By *filters*, a medium made after the deletion started is never deleted.
    """
    done = DeletedMedia()
    # A recording a repair queued or running is made from: kept and counted, as a video's medium is.
    repairing = await retention.repaired_now(db)
    if items is not None:
        for label, model in _MODELS.items():
            ids = sorted({asset_id for kind, asset_id in items if kind == label})
            for start in range(0, len(ids), DELETE_BATCH):
                named = AssetFilters(ids=ids[start : start + DELETE_BATCH])
                used = replace(named, usage="used")
                done.kept += (
                    await db.execute(select(func.count()).select_from(model).where(*_asset_filters(model, label, user, used)))
                ).scalar_one()
                unused = replace(named, usage="unused")
                rows = (await db.execute(select(model).where(*_asset_filters(model, label, user, unused)))).scalars().all()
                held = [row for row in rows if label == "take" and str(row.id) in repairing]
                done.kept += len(held)
                rows = [row for row in rows if row not in held]
                if rows:
                    await _delete_batch(db, [(label, row) for row in rows], done)
        return done
    wanted = filters or AssetFilters()
    started = datetime.now(UTC)
    until = min(wanted.created_to, started) if wanted.created_to else started
    scope = replace(wanted, created_to=until)
    if wanted.usage != "unused":
        in_use = _listing(user, replace(scope, usage="used"))
        done.kept = (await db.execute(select(func.count()).select_from(in_use))).scalar_one()
    if wanted.usage == "used":
        return done
    unused_scope = replace(scope, usage="unused")
    if repairing:
        every = (await db.execute(select(func.count()).select_from(_listing(user, unused_scope)))).scalar_one()
        unused_scope = replace(unused_scope, exclude=[*unused_scope.exclude, *(uuid.UUID(i) for i in repairing)])
    listing = _listing(user, unused_scope)
    left = (await db.execute(select(func.count()).select_from(listing))).scalar_one()
    if repairing:
        done.kept += every - left
    while left > 0:
        batch = await _ranked(db, listing, limit=min(DELETE_BATCH, left))
        if not batch:
            break
        await _delete_batch(db, batch, done)
        left -= len(batch)
    return done


async def used_in(
    db: AsyncSession, user: User, groups: list, media: list[tuple[str, uuid.UUID]]
) -> dict[str, list[dict]]:
    """``{asset id: [{"id", "title"}]}``: the videos *user* can open that use each of *media* (a page's ``(kind, id)``).

    Every such video, however old, the last changed first: one indexed query
    (``retention.uses_media``) reading their ids, titles and references only.
    """
    keys = retention.media_keys(media)
    if not keys:
        return {}
    from app.api.v1.helpers import user_visible_filter

    rows = (
        await db.execute(
            select(VideoProject.id, VideoProject.title, VideoProject.asset_refs)
            .where(user_visible_filter(VideoProject, user.id, groups, is_admin=False), retention.uses_media(keys))
            .order_by(VideoProject.updated_at.desc(), VideoProject.id)
        )
    ).all()
    wanted = set(keys)
    found: dict[str, list[dict]] = {}
    for project_id, title, refs in rows:
        for key in refs or []:
            if key in wanted:
                found.setdefault(key.split(":", 1)[1], []).append({"id": str(project_id), "title": title})
    return found


#: What each kind of medium is called where its deletion is refused.
_MEDIUM_NAMES = {"take": "recording", "clip": "AI shot", "music": "AI track"}


async def deletion_refusal(db: AsyncSession, user: User, kind: str, asset_id: uuid.UUID) -> dict | None:
    """Why deleting one medium a video uses needs ``force``, or ``None`` when no video uses it.

    Every video counts, whoever owns it: a video shared with the creator's
    colleagues loses its scene all the same. The ones *user* can open are
    named (``videos``, the last changed first); the others are counted
    (``others``), never named.
    """
    keys = retention.media_keys([(kind, asset_id)])
    if not keys:
        return None
    if kind == "take" and str(asset_id) in await retention.repaired_now(db):
        # A repair queued or running is made from it: deleting it fails that repair, force or not.
        return {
            "code": "media_in_use",
            "message": (
                "A repair of this recording is queued or running: it cannot be deleted until that repair ended "
                "(force=true does not override it)."
            ),
            "videos": [],
            "others": 0,
            "force_allowed": False,
        }
    total = (
        await db.execute(select(func.count()).select_from(VideoProject).where(retention.uses_media(keys)))
    ).scalar_one()
    if not total:
        return None
    from app.api.v1.helpers import get_user_group_ids

    groups = await get_user_group_ids(db, user.id)
    videos = (await used_in(db, user, groups, [(kind, asset_id)])).get(str(asset_id), [])
    others = max(0, total - len(videos))
    noun = _MEDIUM_NAMES.get(kind, "medium")
    count = f"{total} video{'s' if total != 1 else ''}"
    return {
        "code": "media_in_use",
        "message": (
            f"This {noun} is used in {count}: deleting it leaves its scenes without it, and those videos cannot be "
            "rendered until each scene is given another one. Delete it anyway with force=true."
        ),
        "videos": videos,
        "others": others,
    }
