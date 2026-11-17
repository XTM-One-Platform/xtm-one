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

"""One pass over past conversations' deliverables for the chat ROI (#10544, slice S3).

The recorder hooks (``services/roi_chat_records.py``) write a chat record from
the day they shipped. :func:`backfill_chat_records` replays the same rules
once over what the database already holds, so the deliverables built in chat
before that day (a deck presented weeks ago) count too, on the same low side.

Rules, the ones of the hooks:

* **What.** A presentation, a report or a video saved in a conversation and
  not by an assignment run (``conversation_id`` set, ``assignment_run_id``
  NULL), and a PDF or Word document ``generate_file`` wrote in a conversation
  (``source`` ``agent_working`` / ``agent_download``, no run). One record per
  deliverable, keyed on ``roi_chat.record_key``; a document written again under
  the same name in the same conversation is one deliverable, carried by its
  latest file (the hook's revision move).
* **Size.** Slides, ``report_pages(block_count)``, ``video_minutes`` of the
  planned duration, and one page for a document (its source text is not
  stored: the low side).
* **Use signals.** Only the ones the database still shows: a public link
  (decks and videos) and a grant to a person, a group or the company (decks
  and videos). An export, a presentation or an attachment of the past left no
  trace; such a deliverable counts once it settled (``roi_chat.SETTLE_DAYS``
  after its last edit), which every deliverable still there from before the
  hooks did. A deleted deliverable is gone from its table and gets no record.
* **Idempotent.** ``INSERT ... ON CONFLICT (record_key) DO NOTHING``: a record
  the hooks (or an earlier pass) wrote is never touched, a second pass writes
  nothing. Every row written here carries ``backfilled_at`` and the
  ``created_at`` / ``last_edit_at`` of its deliverable, so the report windows
  place it in its own period.
* **Dry run first.** ``dry_run=True`` answers the counts per kind (scanned,
  to create, already recorded, counting now, the low-bound and counted
  minutes) and writes nothing. The counted minutes subtract the person's
  measured time over the whole conversation (``roi_chat.measured_user_minutes``)
  plus the margin: when a conversation holds several deliverables each one
  subtracts all of it, which can only lower the figure.
* No model call, no quota: a deterministic database pass, paged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi_chat
from app.services.roi_chat_records import DOCUMENT_CONTENT_TYPES, report_pages, video_minutes

logger = logging.getLogger(__name__)

#: Rows read per page: bounds the memory of a pass over a long history.
PAGE_SIZE = 500

#: The ``FileAttachment.source`` values ``generate_file`` stores.
GENERATED_FILE_SOURCES: tuple[str, ...] = ("agent_working", "agent_download")


@dataclass
class KindCounts:
    """What a pass found for one kind of deliverable."""

    scanned: int = 0
    to_create: int = 0
    already_recorded: int = 0
    counted_now: int = 0
    manual_min_low: float = 0.0
    counted_min: float = 0.0


@dataclass
class ChatBackfillResult:
    """What a pass found (``dry_run``) or wrote."""

    dry_run: bool
    kinds: dict[str, KindCounts] = field(default_factory=lambda: {k: KindCounts() for k in roi_chat.KINDS})
    records_created: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["totals"] = {
            "scanned": sum(c.scanned for c in self.kinds.values()),
            "to_create": sum(c.to_create for c in self.kinds.values()),
            "already_recorded": sum(c.already_recorded for c in self.kinds.values()),
            "counted_now": sum(c.counted_now for c in self.kinds.values()),
            "manual_min_low": round(sum(c.manual_min_low for c in self.kinds.values()), 1),
            "counted_min": round(sum(c.counted_min for c in self.kinds.values()), 1),
        }
        for counts in out["kinds"].values():
            counts["manual_min_low"] = round(counts["manual_min_low"], 1)
            counts["counted_min"] = round(counts["counted_min"], 1)
        return out


@dataclass(frozen=True)
class PastDeliverable:
    """One deliverable of the past, as the pass reads it."""

    kind: str
    deliverable_id: str
    conversation_id: uuid.UUID
    user_id: uuid.UUID | None
    title: str
    size: float
    created_at: datetime
    last_edit_at: datetime
    signals: tuple[str, ...] = ()


def _granted(row: Any) -> bool:
    """Whether a deck or a video was given to someone: a person, a group or the whole company."""
    return bool(
        getattr(row, "is_company_managed", False)
        or (getattr(row, "shared_user_ids", None) or [])
        or (getattr(row, "shared_group_ids", None) or [])
    )


def past_signals(*, granted: bool, public_link: bool) -> tuple[str, ...]:
    """The use signals a past deck or video still shows in the database."""
    signals: list[str] = []
    if granted:
        signals.append(roi_chat.SIGNAL_SHARED)
    if public_link:
        signals.append(roi_chat.SIGNAL_PUBLIC_LINK)
    return tuple(signals)


def latest_document_revisions(rows: Iterable[Any]) -> list[Any]:
    """Keep one file per (conversation, file name): the latest, as the hook's revision move does."""
    latest: dict[tuple[Any, str], Any] = {}
    for row in rows:
        key = (row.conversation_id, (row.filename or "").strip())
        held = latest.get(key)
        if held is None or (row.created_at, str(row.id)) > (held.created_at, str(held.id)):
            latest[key] = row
    return list(latest.values())


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _paged(db: AsyncSession, query: Any, id_column: Any) -> Any:
    last: Any = None
    while True:
        page_query = query.order_by(id_column).limit(PAGE_SIZE)
        if last is not None:
            page_query = page_query.where(id_column > last)
        page = (await db.execute(page_query)).all()
        if not page:
            return
        last = page[-1].id
        for row in page:
            yield row


async def _public_link_ids(db: AsyncSession, column: Any, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    return set((await db.execute(select(column).where(column.in_(ids)))).scalars().all())


async def _presentations(db: AsyncSession, now: datetime) -> list[PastDeliverable]:
    from app.models.presentation import Presentation, PresentationPublicLink

    query = select(
        Presentation.id,
        Presentation.conversation_id,
        Presentation.user_id,
        Presentation.title,
        Presentation.slide_count,
        Presentation.is_company_managed,
        Presentation.shared_user_ids,
        Presentation.shared_group_ids,
        Presentation.created_at,
        Presentation.updated_at,
    ).where(Presentation.conversation_id.is_not(None), Presentation.assignment_run_id.is_(None))
    rows = [row async for row in _paged(db, query, Presentation.id)]
    linked = await _public_link_ids(db, PresentationPublicLink.presentation_id, [r.id for r in rows])
    return [
        PastDeliverable(
            kind=roi_chat.KIND_PRESENTATION,
            deliverable_id=str(r.id),
            conversation_id=r.conversation_id,
            user_id=r.user_id,
            title=r.title or "",
            size=float(r.slide_count or 0),
            created_at=_aware(r.created_at, now),
            last_edit_at=_aware(r.updated_at or r.created_at, now),
            signals=past_signals(granted=_granted(r), public_link=r.id in linked),
        )
        for r in rows
    ]


async def _reports(db: AsyncSession, now: datetime) -> list[PastDeliverable]:
    from app.models.report import Report

    query = select(
        Report.id,
        Report.conversation_id,
        Report.user_id,
        Report.title,
        Report.block_count,
        Report.created_at,
        Report.updated_at,
    ).where(Report.conversation_id.is_not(None), Report.assignment_run_id.is_(None))
    return [
        PastDeliverable(
            kind=roi_chat.KIND_REPORT,
            deliverable_id=str(r.id),
            conversation_id=r.conversation_id,
            user_id=r.user_id,
            title=r.title or "",
            size=report_pages(r.block_count),
            created_at=_aware(r.created_at, now),
            last_edit_at=_aware(r.updated_at or r.created_at, now),
        )
        async for r in _paged(db, query, Report.id)
    ]


async def _videos(db: AsyncSession, now: datetime) -> list[PastDeliverable]:
    from app.models.video_studio import VideoProject, VideoPublicLink

    # A video project has no run column: one with a conversation was built in chat.
    query = select(
        VideoProject.id,
        VideoProject.conversation_id,
        VideoProject.user_id,
        VideoProject.title,
        VideoProject.estimated_duration_ms,
        VideoProject.is_company_managed,
        VideoProject.shared_user_ids,
        VideoProject.shared_group_ids,
        VideoProject.created_at,
        VideoProject.updated_at,
    ).where(VideoProject.conversation_id.is_not(None))
    rows = [row async for row in _paged(db, query, VideoProject.id)]
    linked = await _public_link_ids(db, VideoPublicLink.project_id, [r.id for r in rows])
    return [
        PastDeliverable(
            kind=roi_chat.KIND_VIDEO,
            deliverable_id=str(r.id),
            conversation_id=r.conversation_id,
            user_id=r.user_id,
            title=r.title or "",
            size=video_minutes(r.estimated_duration_ms),
            created_at=_aware(r.created_at, now),
            last_edit_at=_aware(r.updated_at or r.created_at, now),
            signals=past_signals(granted=_granted(r), public_link=r.id in linked),
        )
        for r in rows
    ]


async def _documents(db: AsyncSession, now: datetime) -> list[PastDeliverable]:
    from app.models.file_attachment import FileAttachment

    query = select(
        FileAttachment.id,
        FileAttachment.conversation_id,
        FileAttachment.user_id,
        FileAttachment.filename,
        FileAttachment.content_type,
        FileAttachment.created_at,
    ).where(
        FileAttachment.conversation_id.is_not(None),
        FileAttachment.assignment_run_id.is_(None),
        FileAttachment.source.in_(GENERATED_FILE_SOURCES),
    )
    rows = [
        r
        async for r in _paged(db, query, FileAttachment.id)
        if (r.content_type or "").split(";", 1)[0].strip().lower() in DOCUMENT_CONTENT_TYPES
    ]
    return [
        PastDeliverable(
            kind=roi_chat.KIND_FILE,
            deliverable_id=str(r.id),
            conversation_id=r.conversation_id,
            user_id=r.user_id,
            title=(r.filename or "").strip()[:300],
            # The source text of a past document is not stored: one page, the low side.
            size=1.0,
            created_at=_aware(r.created_at, now),
            last_edit_at=_aware(r.created_at, now),
        )
        for r in latest_document_revisions(rows)
    ]


async def _recorded_keys(db: AsyncSession, keys: list[str]) -> set[str]:
    from app.models.roi import RoiChatRecord

    found: set[str] = set()
    for start in range(0, len(keys), PAGE_SIZE):
        chunk = keys[start : start + PAGE_SIZE]
        found.update(
            (await db.execute(select(RoiChatRecord.record_key).where(RoiChatRecord.record_key.in_(chunk))))
            .scalars()
            .all()
        )
    return found


async def user_minutes(db: AsyncSession, conversation_ids: set[uuid.UUID]) -> dict[uuid.UUID, float]:
    """The person's measured time per conversation: the capped gaps between their messages.

    Shared with the chat series read (``services/roi_chat_series.py``, S4).
    """
    from app.models.chat import Message

    times: dict[uuid.UUID, list[datetime]] = {}
    ids = list(conversation_ids)
    for start in range(0, len(ids), PAGE_SIZE):
        chunk = ids[start : start + PAGE_SIZE]
        rows = await db.execute(
            select(Message.conversation_id, Message.created_at).where(
                Message.conversation_id.in_(chunk), Message.role == "user"
            )
        )
        for conversation_id, created_at in rows.all():
            if created_at is not None:
                times.setdefault(conversation_id, []).append(created_at)
    return {cid: roi_chat.measured_user_minutes(stamps) for cid, stamps in times.items()}


def tally(
    result: ChatBackfillResult,
    deliverable: PastDeliverable,
    *,
    recorded: bool,
    user_min: float,
    now: datetime,
) -> bool:
    """Count one deliverable into *result*; True when the pass is to write its record."""
    counts = result.kinds[deliverable.kind]
    counts.scanned += 1
    if recorded:
        counts.already_recorded += 1
        return False
    counts.to_create += 1
    if roi_chat.is_counted(deliverable.signals, last_edit_at=deliverable.last_edit_at, now=now):
        figures = roi_chat.estimate(deliverable.kind, deliverable.size, user_min=user_min)
        counts.counted_now += 1
        counts.manual_min_low += figures.manual_min_low
        counts.counted_min += figures.counted_min
    return True


def _insert_statement(deliverable: PastDeliverable, *, now: datetime) -> Any:
    from app.models.chat import Conversation
    from app.models.roi import RoiChatRecord

    signals = list(deliverable.signals)
    agent = select(Conversation.agent_id).where(Conversation.id == deliverable.conversation_id).scalar_subquery()
    return (
        pg_insert(RoiChatRecord)
        .values(
            id=uuid.uuid4(),
            record_key=roi_chat.record_key(deliverable.kind, deliverable.deliverable_id),
            kind=deliverable.kind,
            deliverable_id=deliverable.deliverable_id,
            user_id=deliverable.user_id,
            agent_id=agent,
            conversation_id=deliverable.conversation_id,
            title=deliverable.title[:300],
            size=max(deliverable.size, 0.0),
            signals=signals,
            # The first signal of the past is dated at the last edit: its own time was not kept.
            first_signal_at=deliverable.last_edit_at if signals else None,
            last_edit_at=deliverable.last_edit_at,
            backfilled_at=now,
            created_at=deliverable.created_at,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[RoiChatRecord.record_key])
        .returning(RoiChatRecord.id)
    )


async def backfill_chat_records(
    db: AsyncSession, *, dry_run: bool = True, now: datetime | None = None
) -> ChatBackfillResult:
    """Record every past chat deliverable that has no record yet; ``dry_run`` writes nothing.

    The caller commits (or rolls back after a dry run).
    """
    now = now or datetime.now(UTC)
    result = ChatBackfillResult(dry_run=dry_run)
    deliverables = [
        *await _presentations(db, now),
        *await _reports(db, now),
        *await _videos(db, now),
        *await _documents(db, now),
    ]
    recorded = await _recorded_keys(db, [roi_chat.record_key(d.kind, d.deliverable_id) for d in deliverables])
    minutes = await user_minutes(db, {d.conversation_id for d in deliverables})
    for deliverable in deliverables:
        key = roi_chat.record_key(deliverable.kind, deliverable.deliverable_id)
        write = tally(
            result,
            deliverable,
            recorded=key in recorded,
            user_min=minutes.get(deliverable.conversation_id, 0.0),
            now=now,
        )
        if not write or dry_run:
            continue
        async with db.begin_nested():
            written = (await db.execute(_insert_statement(deliverable, now=now))).scalar()
        if written is not None:
            result.records_created += 1
    logger.info("ROI chat backfill (dry_run=%s): %s", dry_run, result.as_dict()["totals"])
    return result
