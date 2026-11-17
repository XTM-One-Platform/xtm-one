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

"""Recorder of chat deliverables for ROI (#10544, slice S2; ``dev-docs/roi-outcomes.md`` section 11).

The hooks of the deliverable writers call this module; it writes
``roi_chat_records`` (``models.roi.RoiChatRecord``) and nothing else. The rules
(what counts, how much) stay in the pure module ``services/roi_chat.py`` and
are applied on read, so a row only carries facts: the deliverable, its size as
of its last edit, the use signals seen, its deletion.

* :func:`record_deliverable` upserts the row of a deliverable saved in a
  conversation, keyed on ``roi_chat.record_key``: the first save creates it,
  every later round refreshes its title, size and ``last_edit_at``. A
  deliverable written outside a conversation (an assignment run) is
  automation, counted by ``services/roi_records.py``: no row.
* :func:`record_signal` appends a use signal (``roi_chat.USE_SIGNALS``) once
  and stamps the first one; a deliverable with no row is left alone.
* :func:`record_signal_detached` does the same in a short session of its own,
  for an export that renders outside the request's session (slice S2b).
* :func:`record_deleted` stamps the deletion (decks and videos, slice S2b); ``roi_chat.is_counted`` decides
  whether the work had counted before it.
* :func:`record_document_file` creates the row of a generated document file
  (a PDF or Word document written by ``generate_file``) in a session of its
  own, since the tool runs after the loop released its connection; a
  re-render under the same name moves the row to the new file (S2c).
* :func:`record_sharing` appends ``shared`` when a deck's or a video's grant
  write gave a new person or group access (S2c); :func:`record_attached`
  appends ``attached`` to the files an integration tool takes to send or
  upload (S2c).

Every write runs in a SAVEPOINT and never raises: ROI bookkeeping must never
fail the save, the export or the link it observes. Nothing is committed here,
the caller's transaction carries the row.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi_chat

logger = logging.getLogger(__name__)

#: A report block is about a fifth of a page; a report counts at least one page.
REPORT_BLOCKS_PER_PAGE = 5


def report_pages(block_count: int | None) -> float:
    """The pages a report of *block_count* blocks is sized at, rounded down to keep the figure low."""
    blocks = max(int(block_count or 0), 0)
    if blocks == 0:
        return 0.0
    return float(max(math.floor(blocks / REPORT_BLOCKS_PER_PAGE), 1))


def video_minutes(duration_ms: int | None) -> float:
    """The minutes of video a project is sized at (its planned duration)."""
    return max(float(duration_ms or 0), 0.0) / 60000.0


#: A page of a generated document, in characters of its source text (a dense page).
DOCUMENT_CHARS_PER_PAGE = 3000

#: The generated files that are documents a person would have written by hand.
DOCUMENT_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


def document_pages(source_chars: int | None) -> float:
    """The pages a generated document is sized at: whole dense pages of its source, at least one.

    Rounded down, and a document whose source is unknown (bytes handed in
    already rendered) counts one page: the figure stays on the low side.
    """
    chars = max(int(source_chars or 0), 0)
    return float(max(chars // DOCUMENT_CHARS_PER_PAGE, 1))


def _uuid(value: object) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _guarded(db: AsyncSession, what: str, key: str, statement: Any) -> bool:
    """Run *statement* in a savepoint; log and swallow any failure. True when it ran."""
    try:
        async with db.begin_nested():
            await db.execute(statement)
    except Exception:  # bookkeeping never fails the observed write
        logger.warning("roi chat record: %s failed for %s", what, key, exc_info=True)
        return False
    return True


async def record_deliverable(
    db: AsyncSession,
    *,
    kind: str,
    deliverable_id: object,
    conversation_id: object,
    user_id: object,
    title: str | None,
    size: float,
    at: datetime | None = None,
) -> bool:
    """Create or refresh the row of a deliverable saved in a conversation; no conversation, no row."""
    conv = _uuid(conversation_id)
    if conv is None or kind not in roi_chat.KINDS or deliverable_id is None:
        return False
    from app.models.chat import Conversation
    from app.models.roi import RoiChatRecord

    key = roi_chat.record_key(kind, str(deliverable_id))
    now = at or datetime.now(UTC)
    from app.services.fleet_runs import home_fleet_id_subquery

    agent = select(Conversation.agent_id).where(Conversation.id == conv).scalar_subquery()
    values = {
        "id": uuid.uuid4(),
        "record_key": key,
        "kind": kind,
        "deliverable_id": str(deliverable_id),
        "user_id": _uuid(user_id),
        "agent_id": agent,
        # The agent's home fleet at the first write (#12077), never moved by a later edit.
        "fleet_id": home_fleet_id_subquery(agent),
        "fleet_backfilled": False,
        "conversation_id": conv,
        "title": (title or "")[:300],
        "size": max(float(size or 0), 0.0),
        "last_edit_at": now,
        "created_at": now,
        "updated_at": now,
    }
    statement = (
        pg_insert(RoiChatRecord)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[RoiChatRecord.record_key],
            set_={
                "title": values["title"],
                "size": values["size"],
                "last_edit_at": now,
                "updated_at": now,
            },
        )
    )
    return await _guarded(db, "save", key, statement)


async def record_signal(
    db: AsyncSession,
    *,
    kind: str,
    deliverable_id: object,
    signal: str,
    at: datetime | None = None,
) -> bool:
    """Append *signal* to a deliverable's row once, stamping the first signal; no row, nothing."""
    if kind not in roi_chat.KINDS or signal not in roi_chat.USE_SIGNALS or deliverable_id is None:
        return False
    from app.models.roi import RoiChatRecord

    key = roi_chat.record_key(kind, str(deliverable_id))
    now = at or datetime.now(UTC)
    one = func.jsonb_build_array(signal)
    statement = (
        update(RoiChatRecord)
        .where(RoiChatRecord.record_key == key)
        .where(~RoiChatRecord.signals.contains(one))
        .values(
            signals=RoiChatRecord.signals.op("||")(one),
            first_signal_at=func.coalesce(RoiChatRecord.first_signal_at, now),
            updated_at=now,
        )
    )
    return await _guarded(db, f"signal {signal}", key, statement)


async def record_signal_detached(*, kind: str, deliverable_id: object, signal: str) -> bool:
    """Record *signal* in a session of its own, committed; never raises.

    For the paths that observe a use after releasing their pooled connection
    (an export renders for seconds outside the request's session): the signal
    is written in a short transaction of its own instead.
    """
    if kind not in roi_chat.KINDS or signal not in roi_chat.USE_SIGNALS or deliverable_id is None:
        return False
    from app.core.database import async_session

    try:
        async with async_session() as db:
            ok = await record_signal(db, kind=kind, deliverable_id=deliverable_id, signal=signal)
            await db.commit()
    except Exception:  # bookkeeping never fails the observed export
        logger.warning(
            "roi chat record: detached signal %s failed for %s:%s", signal, kind, deliverable_id, exc_info=True
        )
        return False
    return ok


def _revision_move(*, conv: uuid.UUID, file_id: str, title: str, size: float, now: datetime) -> Any:
    """The statement that moves a document's record to its new revision (same name, same conversation)."""
    from app.models.roi import RoiChatRecord

    return (
        update(RoiChatRecord)
        .where(
            RoiChatRecord.conversation_id == conv,
            RoiChatRecord.kind == roi_chat.KIND_FILE,
            RoiChatRecord.title == title,
            RoiChatRecord.deleted_at.is_(None),
        )
        .values(
            record_key=roi_chat.record_key(roi_chat.KIND_FILE, file_id),
            deliverable_id=file_id,
            size=size,
            last_edit_at=now,
            updated_at=now,
        )
    )


async def record_document_file(
    *,
    file_id: object,
    conversation_id: object,
    user_id: object,
    filename: str,
    content_type: str | None,
    source_chars: int | None,
) -> bool:
    """Record a document file ``generate_file`` wrote in a conversation, in a session of its own; never raises.

    Only a PDF or a Word document (:data:`DOCUMENT_CONTENT_TYPES`) is a
    deliverable; any other file opens no session. A document written again
    under the same name in the same conversation is a new revision of the same
    deliverable: its record moves to the new file id (so a later ``attached``
    finds it) instead of counting twice. The tool runs after the agentic loop
    released its pooled connection, so the row is written in a short
    transaction of its own, like :func:`record_signal_detached`.
    """
    conv = _uuid(conversation_id)
    base_type = (content_type or "").split(";", 1)[0].strip().lower()
    fid = _uuid(file_id)
    if conv is None or fid is None or base_type not in DOCUMENT_CONTENT_TYPES:
        return False
    from app.core.database import async_session

    title = (filename or "").strip()[:300]
    size = document_pages(source_chars)
    now = datetime.now(UTC)
    try:
        async with async_session() as db:
            moved = await db.execute(_revision_move(conv=conv, file_id=str(fid), title=title, size=size, now=now))
            if getattr(moved, "rowcount", 0):
                ok = True
            else:
                ok = await record_deliverable(
                    db,
                    kind=roi_chat.KIND_FILE,
                    deliverable_id=fid,
                    conversation_id=conv,
                    user_id=user_id,
                    title=title,
                    size=size,
                    at=now,
                )
            await db.commit()
    except Exception:  # bookkeeping never fails the observed write
        logger.warning("roi chat record: document file %s not recorded", file_id, exc_info=True)
        return False
    return ok


def shared_kind(entity: object) -> str | None:
    """The chat kind of a shared entity: a deck or a video; None for anything else (a dashboard)."""
    from app.models.presentation import Presentation
    from app.models.video_studio import VideoProject

    if isinstance(entity, Presentation):
        return roi_chat.KIND_PRESENTATION
    if isinstance(entity, VideoProject):
        return roi_chat.KIND_VIDEO
    return None


async def record_sharing(db: AsyncSession, entity: object, audit: dict[str, Any]) -> bool:
    """Append ``shared`` when a grant write on a deck or a video gave someone access.

    *audit* is ``share_principals.grants_audit`` of the write: only an added
    person or group is a use; a removal or a tier change is not.
    """
    kind = shared_kind(entity)
    if kind is None or not (audit.get("added_users") or audit.get("added_groups")):
        return False
    return await record_signal(db, kind=kind, deliverable_id=getattr(entity, "id", None), signal=roi_chat.SIGNAL_SHARED)


async def record_attached(files: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> int:
    """Append ``attached`` to the chat records of the files an integration tool sends; never raises.

    *files* are ``(file_id, content_type)`` pairs. Only a generated document
    (:data:`DOCUMENT_CONTENT_TYPES`) can carry a record, so any other file
    opens no session; a document with no record (an upload, a download) is
    left alone by :func:`record_signal`. Returns how many records it marked.
    """
    ids = (
        str(file_id).strip().removeprefix("file-")
        for file_id, content_type in files or ()
        if (content_type or "").split(";", 1)[0].strip().lower() in DOCUMENT_CONTENT_TYPES
    )
    marked = 0
    for file_id in dict.fromkeys(ids):
        if _uuid(file_id) is None:
            continue
        if await record_signal_detached(kind=roi_chat.KIND_FILE, deliverable_id=file_id, signal=roi_chat.SIGNAL_ATTACHED):
            marked += 1
    return marked


async def record_deleted(
    db: AsyncSession,
    *,
    kind: str,
    deliverable_id: object,
    at: datetime | None = None,
) -> bool:
    """Stamp the deletion of a deliverable on its row (the first deletion wins)."""
    if kind not in roi_chat.KINDS or deliverable_id is None:
        return False
    from app.models.roi import RoiChatRecord

    key = roi_chat.record_key(kind, str(deliverable_id))
    now = at or datetime.now(UTC)
    statement = (
        update(RoiChatRecord)
        .where(RoiChatRecord.record_key == key, RoiChatRecord.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
    )
    return await _guarded(db, "delete", key, statement)
