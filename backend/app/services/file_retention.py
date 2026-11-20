# SPDX-License-Identifier: Apache-2.0

"""File retention: how long each category of stored file is kept, and the sweep that applies it.

Every ``FileAttachment`` belongs to a category by its ``source``
(:data:`CATEGORIES`). A category has a number of days an administrator sets in
Settings > Storage & retention (``PlatformSettings.file_retention``), seeded
from its environment variable; 0 keeps its files for good ("Never"). An
environment variable set explicitly is a hard lock: the value is shown, not
editable (:func:`load_policy`).

The days of ``agent_working`` and ``agent_download`` (the agent's scratch and
delivered files, 60 by default) count from the file's LAST USE
(``last_accessed_at``, touched by :func:`touch`; NULL reads as
``created_at``). Every other category counts from its creation.

The sweep (:func:`sweep`, hourly, bounded per tick) never deletes a file a live
object still names (:func:`referenced`): an open work item's attachment, a
workset item, an active shared state, a presentation or report spec, a video.
It keeps the row as a TOMBSTONE (``deleted_at``, ``deleted_reason``) and drops
only the bytes, rows committed first, objects purged after
(``file_cleanup.purge_storage_objects``): every surface that shows the file
draws a "deleted" card (:func:`deleted_notice`) and the download routes answer
``410 Gone`` (:func:`gone_detail`).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Text, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Why a file's bytes are gone (``FileAttachment.deleted_reason``).
DELETED_RETENTION = "retention"
DELETED_OWNER = "owner_deleted"
DELETED_ADMIN = "admin"
DELETED_REASONS = (DELETED_RETENTION, DELETED_OWNER, DELETED_ADMIN)

#: The most days a category keeps a file (about ten years): what Settings accepts.
MAX_DAYS = 3650
#: Rows one sweep pass reads per category; one tick never holds a long transaction.
SWEEP_BATCH = 200
#: Most files one tick deletes per category; the rest go at the next tick.
SWEEP_MAX_PER_TICK = 2000
#: A use is written at most this often per file (a chat re-reads its files on every turn).
TOUCH_EVERY = timedelta(hours=6)
#: Nothing younger is ever deleted, whatever the days: work in flight.
FLOOR = timedelta(hours=1)
#: Redis key of the last sweep's figures, read by Settings.
_LAST_SWEEP_KEY = "file_retention:last_sweep"


@dataclass(frozen=True)
class Category:
    """One category of stored files: which sources it holds and how long they stay."""

    key: str
    #: Exact ``FileAttachment.source`` values; :attr:`prefix` adds every source starting with it.
    sources: tuple[str, ...]
    prefix: str | None
    #: The ``Settings`` field holding the default days (its environment variable, upper-cased).
    setting: str
    #: ``last_use``: days count from the last use; ``created``: from creation.
    basis: str
    #: Swept by this module (False: an older sweep owns it and reads the days from here).
    swept_here: bool = True

    @property
    def env_var(self) -> str:
        return self.setting.upper()


CATEGORIES: tuple[Category, ...] = (
    Category("agent_working", ("agent_working",), None, "file_retention_agent_working_days", "last_use"),
    Category("agent_download", ("agent_download",), None, "file_retention_agent_download_days", "last_use"),
    Category("agent", ("agent", "agent_generated", "mcp"), None, "file_retention_agent_days", "created"),
    Category("chat", ("chat",), None, "file_retention_chat_days", "created"),
    Category("channel", (), "channel:", "file_retention_channel_days", "created"),
    Category("agent_check", ("agent_check",), None, "agent_check_file_retention_days", "created", swept_here=False),
)
BY_KEY: dict[str, Category] = {category.key: category for category in CATEGORIES}


def _now() -> datetime:
    return datetime.now(UTC)


def _bounded(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return min(max(int(raw), 0), MAX_DAYS)


def default_days(category: Category) -> int:
    """The category's environment value (its seed)."""
    from app.core.config import settings

    return _bounded(getattr(settings, category.setting, 0)) or 0


def locked(category: Category) -> bool:
    """Whether the environment sets the category explicitly: a hard lock Settings shows, never overrides."""
    return category.env_var in os.environ


@dataclass(frozen=True)
class Rule:
    """The days a category keeps its files now, where they come from."""

    category: Category
    days: int
    source: str  # "default" | "settings" | "environment"

    @property
    def locked(self) -> bool:
        return self.source == "environment"

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.key,
            "days": self.days,
            "never": self.days == 0,
            "basis": self.category.basis,
            "source": self.source,
            "locked": self.locked,
            "default_days": default_days(self.category),
            "env_var": self.category.env_var,
        }


def policy_of(stored: object) -> dict[str, Rule]:
    """The rules for a stored ``PlatformSettings.file_retention`` value: env lock, then stored, then default."""
    stored_map = stored if isinstance(stored, Mapping) else {}
    rules: dict[str, Rule] = {}
    for category in CATEGORIES:
        if locked(category):
            rules[category.key] = Rule(category, default_days(category), "environment")
            continue
        value = _bounded(stored_map.get(category.key))
        if value is None:
            rules[category.key] = Rule(category, default_days(category), "default")
        else:
            rules[category.key] = Rule(category, value, "settings")
    return rules


async def load_policy(db: AsyncSession) -> dict[str, Rule]:
    """The platform's file retention, per category."""
    from app.models.platform_settings import PlatformSettings

    stored = (await db.execute(select(PlatformSettings.file_retention).limit(1))).scalar_one_or_none()
    return policy_of(stored)


async def days_for(db: AsyncSession, key: str) -> int:
    """The days category *key* keeps a file now; 0 = kept for good."""
    return (await load_policy(db))[key].days


class RetentionLockedError(ValueError):
    """A write named a category the environment locks."""


async def save_policy(db: AsyncSession, values: Mapping[str, object]) -> dict[str, dict[str, int]]:
    """Store the days given in *values* (the others unchanged); ``{category: {from, to}}`` changed. Does not commit.

    Raises :class:`KeyError` for an unknown category, :class:`RetentionLockedError`
    for one the environment locks, :class:`ValueError` for a value that is not
    a whole number of days between 0 and :data:`MAX_DAYS`.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings

    row = await get_platform_settings(db)
    before = policy_of(row.file_retention)
    stored = dict(row.file_retention or {})
    changed: dict[str, dict[str, int]] = {}
    for key, raw in values.items():
        if key not in BY_KEY:
            raise KeyError(key)
        if before[key].locked:
            raise RetentionLockedError(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_DAYS:
            raise ValueError(f"{key}: a whole number of days between 0 and {MAX_DAYS}")
        stored[key] = raw
        if before[key].days != raw:
            changed[key] = {"from": before[key].days, "to": raw}
    row.file_retention = stored
    return changed


# ── Which files a category holds ──────────────────────────────────────────


def source_clause(category: Category) -> ColumnElement[bool]:
    """SQL: the files of *category*, by ``FileAttachment.source``."""
    from app.models.file_attachment import FileAttachment

    parts: list[ColumnElement[bool]] = []
    if category.sources:
        parts.append(FileAttachment.source.in_(category.sources))
    if category.prefix:
        parts.append(FileAttachment.source.startswith(category.prefix))
    return or_(*parts)


def category_of(source: object) -> Category | None:
    """The category a ``FileAttachment.source`` belongs to, ``None`` for one no rule covers (template assets...)."""
    value = str(source or "")
    for category in CATEGORIES:
        if value in category.sources or (category.prefix and value.startswith(category.prefix)):
            return category
    return None


def _age_column(category: Category) -> ColumnElement[Any]:
    from app.models.file_attachment import FileAttachment

    if category.basis == "last_use":
        return func.coalesce(FileAttachment.last_accessed_at, FileAttachment.created_at)
    return FileAttachment.created_at


def cutoff(days: int, *, now: datetime | None = None) -> datetime | None:
    """The moment before which a file is due; ``None`` when *days* keeps them for good."""
    if days <= 0:
        return None
    moment = now or _now()
    return min(moment - timedelta(days=days), moment - FLOOR)


def expires_at(row: Any, days: int, basis: str) -> datetime | None:
    """When an unused *row* goes under *days* (``None``: never, or already deleted)."""
    if days <= 0 or getattr(row, "deleted_at", None) is not None:
        return None
    start = getattr(row, "created_at", None)
    if basis == "last_use":
        start = getattr(row, "last_accessed_at", None) or start
    if not isinstance(start, datetime):
        return None
    return start + timedelta(days=days)


# ── What keeps a file ─────────────────────────────────────────────────────


async def referenced(db: AsyncSession, file_ids: Iterable[object]) -> set[str]:
    """The ids of *file_ids* a live object still names: the sweep keeps them.

    An open work item's attachment, a workset item, an active shared state's
    value, a presentation or report spec, a video (``video_studio.retention``).
    A chat message is not one: a message keeps showing its file as a tombstone
    card, which is the point of the retention.
    """
    from app.models.presentation import Presentation
    from app.models.report import Report
    from app.models.shared_state import SharedState
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem
    from app.models.workset import WorksetItem
    from app.services.video_studio.retention import files_in_use

    ids = [str(fid) for fid in file_ids if fid]
    if not ids:
        return set()
    found: set[str] = set(await files_in_use(db, ids))
    uuids = [uuid.UUID(fid) for fid in ids]
    found |= {
        str(fid)
        for fid in (
            await db.execute(select(WorksetItem.file_attachment_id).where(WorksetItem.file_attachment_id.in_(uuids)))
        ).scalars()
        if fid
    }
    remaining = [fid for fid in ids if fid not in found]
    text_columns = (
        (cast(WorkItem.attachment_file_ids, Text), WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES)),
        (cast(SharedState.value, Text), SharedState.status == "active"),
        (cast(Presentation.spec, Text), None),
        (cast(Report.spec, Text), None),
    )
    for column, live in text_columns:
        if not remaining:
            break
        for fid in list(remaining):
            query = select(func.count()).where(column.contains(fid))
            if live is not None:
                query = query.where(live)
            if (await db.execute(query.limit(1))).scalar():
                found.add(fid)
                remaining.remove(fid)
    return found


# ── The sweep ─────────────────────────────────────────────────────────────


async def due(
    db: AsyncSession,
    category: Category,
    days: int,
    *,
    now: datetime | None = None,
    limit: int = SWEEP_BATCH,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[tuple[uuid.UUID, datetime, int]]:
    """``(id, age, size)`` of the files of *category* past *days*, not deleted yet, oldest first."""
    from sqlalchemy import tuple_

    from app.models.file_attachment import FileAttachment

    before = cutoff(days, now=now)
    if before is None:
        return []
    age = _age_column(category)
    query = select(FileAttachment.id, age, FileAttachment.size).where(
        source_clause(category), FileAttachment.deleted_at.is_(None), age < before
    )
    if after is not None:
        query = query.where(tuple_(age, FileAttachment.id) > after)
    rows = (await db.execute(query.order_by(age, FileAttachment.id).limit(limit))).all()
    return [(row[0], row[1], int(row[2] or 0)) for row in rows]


async def tombstone(
    db: AsyncSession, file_ids: list[uuid.UUID], reason: str, *, now: datetime | None = None
) -> list[str]:
    """Mark *file_ids* deleted for *reason*; their object keys, for :func:`purge_storage_objects` after the commit."""
    from app.models.file_attachment import FileAttachment

    if not file_ids:
        return []
    result = await db.execute(
        update(FileAttachment)
        .where(FileAttachment.id.in_(file_ids), FileAttachment.deleted_at.is_(None))
        .values(deleted_at=now or _now(), deleted_reason=reason)
        .returning(FileAttachment.s3_object_name)
        .execution_options(synchronize_session=False)
    )
    return [key for key in result.scalars().all() if key]


async def sweep_category(
    db: AsyncSession,
    category: Category,
    days: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    limit: int = SWEEP_MAX_PER_TICK,
) -> dict[str, Any]:
    """Delete (or count, *dry_run*) the files of *category* past *days*: ``{files, bytes, kept_referenced, more}``."""
    from app.services.file_cleanup import purge_storage_objects

    tally: dict[str, Any] = {"files": 0, "bytes": 0, "kept_referenced": 0, "objects_left": 0, "more": False}
    after: tuple[datetime, uuid.UUID] | None = None
    seen = 0
    while seen < limit:
        rows = await due(db, category, days, now=now, limit=min(SWEEP_BATCH, limit - seen), after=after)
        if not rows:
            break
        seen += len(rows)
        after = (rows[-1][1], rows[-1][0])
        kept = await referenced(db, [row[0] for row in rows])
        tally["kept_referenced"] += len(kept)
        wanted = [row for row in rows if str(row[0]) not in kept]
        tally["files"] += len(wanted)
        tally["bytes"] += sum(row[2] for row in wanted)
        if dry_run or not wanted:
            continue
        keys = await tombstone(db, [row[0] for row in wanted], DELETED_RETENTION, now=now)
        await db.commit()
        tally["objects_left"] += len(keys) - await purge_storage_objects(keys)
    if seen >= limit:
        tally["more"] = True
    return tally


async def sweep(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    also: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """One tick of the file retention over every category this module sweeps; per category its tally. Commits.

    *also* names the files an older sweep deleted in the same pass per category
    (the check pictures), kept with the figures Settings shows as the last sweep.
    """
    rules = await load_policy(db)
    report: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        if not category.swept_here:
            continue
        days = rules[category.key].days
        if days <= 0:
            continue
        report[category.key] = await sweep_category(db, category, days, now=now, dry_run=dry_run)
    for key, files in (also or {}).items():
        if key in BY_KEY and key not in report:
            report[key] = {"files": int(files), "bytes": 0, "kept_referenced": 0, "objects_left": 0, "more": False}
    if not dry_run:
        await _remember_sweep(report, now or _now())
    return report


async def preview(
    db: AsyncSession, values: Mapping[str, int], *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """What the next sweep would delete per category under *values* (the stored days elsewhere); nothing written."""
    rules = await load_policy(db)
    report: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        days = values.get(category.key, rules[category.key].days)
        if days <= 0:
            report[category.key] = {"files": 0, "bytes": 0, "kept_referenced": 0, "more": False}
            continue
        report[category.key] = await sweep_category(db, category, int(days), now=now, dry_run=True)
    return report


async def _remember_sweep(report: Mapping[str, Mapping[str, Any]], at: datetime) -> None:
    import json

    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        body = {
            "at": at.isoformat(),
            "files": {key: int(tally.get("files", 0)) for key, tally in report.items()},
            "bytes": {key: int(tally.get("bytes", 0)) for key, tally in report.items()},
        }
        await redis.set(_LAST_SWEEP_KEY, json.dumps(body))
    except Exception:  # the figures are a courtesy for Settings, never a reason to fail the sweep
        logger.debug("File retention: the last sweep's figures were not kept", exc_info=True)


async def last_sweep() -> dict[str, Any] | None:
    """The figures of the last sweep (``{at, files, bytes}``), ``None`` when none is known."""
    import json

    try:
        from app.core.redis import redis_client

        raw = await (await redis_client()).get(_LAST_SWEEP_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # an unreachable Redis reads as no sweep known
        return None


async def holdings(db: AsyncSession) -> dict[str, dict[str, int]]:
    """Per category, the live files and bytes it holds today, and its tombstones."""
    from app.models.file_attachment import FileAttachment

    out: dict[str, dict[str, int]] = {}
    for category in CATEGORIES:
        live, size, gone = (
            await db.execute(
                select(
                    func.count(FileAttachment.id).filter(FileAttachment.deleted_at.is_(None)),
                    func.coalesce(func.sum(FileAttachment.size).filter(FileAttachment.deleted_at.is_(None)), 0),
                    func.count(FileAttachment.id).filter(FileAttachment.deleted_at.is_not(None)),
                ).where(source_clause(category))
            )
        ).one()
        out[category.key] = {"files": int(live), "bytes": int(size), "deleted": int(gone)}
    return out


# ── A use, and the deleted file ───────────────────────────────────────────


async def touch(db: AsyncSession, file_ids: Iterable[object], *, now: datetime | None = None) -> None:
    """Record a use of *file_ids* (download, preview, read, attach). Throttled; never raises; does not commit."""
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment

    ids = [fid for fid in (safe_uuid(str(f)) for f in file_ids if f) if fid is not None]
    if not ids:
        return
    moment = now or _now()
    try:
        await db.execute(
            update(FileAttachment)
            .where(
                FileAttachment.id.in_(ids),
                FileAttachment.deleted_at.is_(None),
                or_(FileAttachment.last_accessed_at.is_(None), FileAttachment.last_accessed_at < moment - TOUCH_EVERY),
            )
            .values(last_accessed_at=moment)
            .execution_options(synchronize_session=False)
        )
    except Exception:  # a use not recorded is never a reason to fail the read
        logger.debug("File retention: a use was not recorded", exc_info=True)


#: Process-local: when this process last recorded a use of a file, so a tool reading it ten times writes once.
_TOUCHED: dict[str, datetime] = {}
_TOUCHED_MAX = 10_000
_PENDING: set[Any] = set()


async def _touch_in_own_session(ids: list[str]) -> None:
    try:
        from app.core.database import async_session

        async with async_session() as db:
            await touch(db, ids)
            await db.commit()
    except Exception:  # a use not recorded is never a reason to fail the read
        logger.debug("File retention: a use was not recorded", exc_info=True)


async def touch_now(file_ids: Iterable[object]) -> None:
    """:func:`touch` for a caller holding no session (a tool's read): in the background, throttled per process.

    The read never waits on it: the write runs as its own task with its own
    short transaction.
    """
    import asyncio

    moment = _now()
    ids = []
    for fid in file_ids:
        key = str(fid or "")
        if not key:
            continue
        last = _TOUCHED.get(key)
        if last is not None and moment - last < TOUCH_EVERY:
            continue
        ids.append(key)
        _TOUCHED[key] = moment
    if len(_TOUCHED) > _TOUCHED_MAX:
        _TOUCHED.clear()
    if not ids:
        return
    task = asyncio.get_running_loop().create_task(_touch_in_own_session(ids))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


def is_deleted(row: Any) -> bool:
    """Whether *row*'s bytes are gone (a tombstone)."""
    return getattr(row, "deleted_at", None) is not None


def deleted_notice(row: Any) -> str:
    """The one sentence a person or an agent reads for a deleted file."""
    when = getattr(row, "deleted_at", None)
    day = when.strftime("%Y-%m-%d") if isinstance(when, datetime) else "an earlier date"
    name = getattr(row, "filename", None) or "This file"
    reason = getattr(row, "deleted_reason", None)
    if reason == DELETED_RETENTION:
        category = category_of(getattr(row, "source", None))
        if category is not None and category.basis == "last_use":
            return f"{name} was deleted on {day} by the retention policy (unused for too long)."
        return f"{name} was deleted on {day} by the retention policy."
    if reason == DELETED_ADMIN:
        return f"{name} was deleted on {day} by an administrator."
    return f"{name} was deleted on {day}."


def gone_detail(row: Any) -> dict[str, Any]:
    """The JSON body of a ``410 Gone`` for a deleted file."""
    when = getattr(row, "deleted_at", None)
    return {
        "code": "file_deleted",
        "message": deleted_notice(row),
        "file_id": str(getattr(row, "id", "") or ""),
        "filename": getattr(row, "filename", None),
        "content_type": getattr(row, "content_type", None),
        "size": int(getattr(row, "size", 0) or 0),
        "deleted_at": when.isoformat() if isinstance(when, datetime) else None,
        "deleted_reason": getattr(row, "deleted_reason", None),
    }


def raise_if_deleted(row: Any) -> None:
    """Raise ``410 Gone`` with :func:`gone_detail` when *row* is a tombstone (a route serving its bytes)."""
    if is_deleted(row):
        from fastapi import HTTPException

        raise HTTPException(status_code=410, detail=gone_detail(row))


def tombstone_fields(row: Any) -> dict[str, Any]:
    """The fields a file listing adds for *row*: ``deleted_at``, ``deleted_reason`` (``None`` when live)."""
    when = getattr(row, "deleted_at", None)
    return {
        "deleted_at": when.isoformat() if isinstance(when, datetime) else None,
        "deleted_reason": getattr(row, "deleted_reason", None),
    }


__all__ = [
    "BY_KEY",
    "CATEGORIES",
    "DELETED_ADMIN",
    "DELETED_OWNER",
    "DELETED_REASONS",
    "DELETED_RETENTION",
    "MAX_DAYS",
    "Category",
    "RetentionLockedError",
    "Rule",
    "category_of",
    "cutoff",
    "deleted_notice",
    "expires_at",
    "gone_detail",
    "holdings",
    "is_deleted",
    "last_sweep",
    "load_policy",
    "policy_of",
    "preview",
    "raise_if_deleted",
    "referenced",
    "save_policy",
    "sweep",
    "sweep_category",
    "tombstone",
    "tombstone_fields",
    "touch",
    "touch_now",
]
