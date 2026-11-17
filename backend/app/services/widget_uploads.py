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

"""Files a visitor of the public chat widget attaches to a message.

A widget visitor has no account. On a widget whose owner allows it
(``Agent.web_uploads_enabled``, off by default; the Security Audit's
``web.public_widget_uploads`` control counts the widgets that allow it) the
visitor may attach files to a message, and the agent receives each one as the
web chat does: a stored file of the conversation, the id in the turn's
``FileContext`` for the tools that take a file, and its content - a document
as its inline text (:func:`document_text`, what the chat upload message
carries), an image as its pixels. Every bound on what an anonymous uploader can
do lives here, and every refusal is an :class:`UploadRefusedError` carrying the
status and the sentence the visitor reads:

* **Kind and size** (:func:`check_upload`, :func:`inspect_upload`): the
  documents and data files the agent reads as text and the raster images it
  sees, decided on the file name's extension - the declared type is ignored,
  the stored type is the platform's own for that extension - with the bytes
  checked to be what the extension says: an image decoded as that format, an
  Office document an OOXML package whose parts and expanded size are bounded
  (``widget_upload_inspection``), no archive under another name. At most the
  chat upload's size (``MAX_UPLOAD_SIZE_BYTES``).
* **Load**: at most :data:`MAX_CONCURRENT_UPLOADS` visitor files handled at
  once per process (:func:`upload_slot`, whose permit a store left running by
  a disconnected visitor keeps until it ends), the per-address and per-widget
  budgets of ``core/rate_limit.py`` in the route (``public_chat``), failing
  closed, and every image and Office file decoded and every document read in a
  worker process that is killed when it overruns (``widget_document_reader``):
  a file it could not finish with is refused, and nothing a visitor sends is
  ever decoded on the platform's shared parse executor.
* **Storage** (:func:`check_room`, :func:`accept_upload`): a session holds at
  most :data:`SESSION_MAX_FILES` files and :data:`SESSION_MAX_BYTES`, a widget
  at most :data:`WIDGET_MAX_BYTES` across its sessions: checked before the body
  is read, again with the file's size before anything decodes it, and again
  under a per-widget transaction lock when the row is written, so parallel
  uploads cannot overshoot. The object and its row are written by a task a
  visitor who disconnects does not stop; when the row cannot be written, the
  object is deleted, or kept as a tombstone row for the sweep when the store
  does not answer, and only when the database cannot take that row either is
  the key left to the error log.
* **Content**: the prompt guard evaluates exactly what the model will read of
  a document (surface ``widget_upload``), audited like a widget message; with
  ``prompt_guard_block_public_widget`` a ``block`` rule refuses the file before
  it is stored. A document never reaches the model as its native bytes, so no
  part of it escapes that evaluation; an image is pixels, not text.
* **Reach**: the row belongs to the widget conversation and to no user, so the
  session's own route is the only one that serves it (raster images inline,
  everything else as an attachment, never kept by a cache) and on the platform
  it stays admins-only.
* **Retention** (:func:`purge_expired_uploads`): deleted with its session, a
  file never sent after :data:`UNSENT_RETENTION`, the others once the session
  has had no message for ``PUBLIC_WIDGET_UPLOAD_RETENTION_DAYS`` - the object
  first, then its row, so a store that does not answer leaves both for the
  next sweep. The same purge deletes the files stored for a Slack or Teams
  message queued behind a running turn that never ran
  (``channel_continuation.expired_queued_files_clause``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import uuid
import weakref
from collections.abc import AsyncIterator, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, func, or_, select, text, update

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type
from app.services.widget_upload_inspection import OOXML_MAIN_PARTS, needs_inspection

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.file_attachment import FileAttachment

logger = logging.getLogger(__name__)

#: ``FileAttachment.source`` of a file a widget visitor attached.
WIDGET_UPLOAD_SOURCE = "widget_upload"

#: What a visitor may attach: documents and data files the agent reads as
#: text, and raster images it sees. SVG is not accepted - the one image type
#: that carries markup - and neither are archives, audio or video, which the
#: agent cannot read in a widget turn.
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "csv",
        "docx",
        "htm",
        "html",
        "json",
        "jsonl",
        "log",
        "md",
        "ndjson",
        "pdf",
        "pptx",
        "tsv",
        "txt",
        "xls",
        "xlsx",
        "xml",
        "yaml",
        "yml",
    }
)
IMAGE_EXTENSIONS: frozenset[str] = frozenset({"gif", "jpeg", "jpg", "png", "webp"})
ACCEPTED_EXTENSIONS: frozenset[str] = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS

MAX_FILE_BYTES = MAX_UPLOAD_SIZE_BYTES
SESSION_MAX_FILES = 10
SESSION_MAX_BYTES = 100 * 1024 * 1024
WIDGET_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: Room for the multipart framing of one file - boundary, part headers, name.
UPLOAD_FRAMING_BYTES = 64 * 1024
MAX_FILENAME_CHARS = 255
#: Upload bodies read and processed at once in one process: each is held in
#: memory while it is checked, screened and stored. Past it a visitor is told
#: to retry, never queued.
MAX_CONCURRENT_UPLOADS = 4
BUSY_RETRY_AFTER_SECS = 5

#: How much of a document the model reads: the chat upload message's cap.
INLINE_MAX_CHARS = 50_000

#: The bound on the guard's evaluation of one file: the visitor waits for it.
GUARD_TIMEOUT_SECS = 20.0

#: A file uploaded and never sent with a message is deleted after this.
UNSENT_RETENTION = timedelta(days=1)
PURGE_BATCH = 200
PURGE_MAX_BATCHES = 25
PURGE_INTERVAL_SECS = 3600
_PURGE_LEASE_KEY = "widget_uploads:purge"

_LOCK_SCOPE = "widget_upload"

UPLOADS_OFF_DETAIL = "This assistant does not accept files."
FILE_GONE_DETAIL = "A file attached to this message is no longer available. Attach it again."
GUARD_REFUSAL_DETAIL = "This file was refused by the platform's content checks."
FORM_DETAIL = "Send the file as the one multipart form part named 'file'."
STORAGE_DOWN_DETAIL = "The file could not be stored right now. Please try again later."
BUSY_DETAIL = "Too many files are being received right now. Please try again in a moment."
UNREADABLE_DETAIL = (
    "This document could not be read: it takes too long or too much memory to read. Try a smaller or simpler file."
)
UNCHECKABLE_DETAIL = (
    "This file could not be checked: it takes too long or too much memory to decode. Try a smaller or simpler file."
)
READER_DOWN_DETAIL = "Files cannot be read right now. Please try again in a moment."

_OOXML_EXTENSIONS = frozenset(OOXML_MAIN_PARTS)
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TEXT_PROBE_BYTES = 8192
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class UploadRefusedError(Exception):
    """A visitor upload the platform does not take, with the answer to give."""

    def __init__(self, status_code: int, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


def uploads_allowed(agent: Any) -> bool:
    """Whether the widget of *agent* lets its visitors attach files."""
    return getattr(agent, "web_uploads_enabled", False) is True


def upload_rules() -> dict[str, Any]:
    """What the widget needs to offer the attach control, served with its config."""
    return {
        "accept": ",".join(f".{ext}" for ext in sorted(ACCEPTED_EXTENSIONS)),
        "max_bytes": MAX_FILE_BYTES,
        "max_files": SESSION_MAX_FILES,
    }


def is_image(content_type: str | None) -> bool:
    """A visitor image reaches the model as its pixels; any other file as its text."""
    return str(content_type or "").startswith("image/")


def _size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / (1024 * 1024):.1f} MB".replace(".0 MB", " MB")


TOO_LARGE_DETAIL = f"The file is too large: the limit is {_size(MAX_FILE_BYTES)}."


def _accepted_list() -> str:
    return ", ".join(f".{ext}" for ext in sorted(ACCEPTED_EXTENSIONS))


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


class UploadPermit:
    """One of the process's upload slots, given back once, by whoever holds it last."""

    def __init__(self, slot: asyncio.Semaphore) -> None:
        self._slot = slot
        self._held = True
        self.detached = False

    def release(self) -> None:
        if self._held:
            self._held = False
            self._slot.release()


@contextlib.asynccontextmanager
async def upload_slot() -> AsyncIterator[UploadPermit]:
    """Hold one of the process's :data:`MAX_CONCURRENT_UPLOADS` slots; refuse (503) when none is free.

    Given back on exit, unless a store its caller left took it over
    (:func:`_finish_even_if_abandoned`): the store then gives it back when it ends.
    """
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    # Checked and taken with no await in between: nothing can take it meanwhile.
    if slot.locked():
        raise UploadRefusedError(503, BUSY_DETAIL, retry_after=BUSY_RETRY_AFTER_SECS)
    await slot.acquire()
    permit = UploadPermit(slot)
    try:
        yield permit
    finally:
        if not permit.detached:
            permit.release()


# ---------------------------------------------------------------------------
# What a visitor may attach
# ---------------------------------------------------------------------------


def clean_filename(raw: str | None) -> str:
    """The name a visitor's file is stored under: no path, no control character, bounded."""
    name = _CONTROL_CHARS.sub("", (raw or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if len(name) <= MAX_FILENAME_CHARS:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 16:
        return name[:MAX_FILENAME_CHARS]
    return f"{stem[: MAX_FILENAME_CHARS - len(ext) - 1]}.{ext}"


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _content_problem(ext: str, data: bytes) -> str | None:
    """Why *data* is not the kind of file its extension names, read off its first bytes, or ``None``."""
    if ext in IMAGE_EXTENSIONS:
        from app.services.image_summary import looks_like_raster_image

        # No declared type: the magic bytes alone decide.
        return None if looks_like_raster_image(data) else f"The file is not a valid {ext.upper()} image."
    if ext == "pdf":
        return None if b"%PDF-" in data[:1024] else "The file is not a valid PDF document."
    if ext in _OOXML_EXTENSIONS:
        return None if data[:4] == _ZIP_MAGIC else f"The file is not a valid {ext.upper()} document."
    if ext == "xls":
        return None if data[:8] == _OLE_MAGIC else "The file is not a valid XLS document."
    probe = data[:_TEXT_PROBE_BYTES]
    if b"\x00" in probe and not probe.startswith((b"\xff\xfe", b"\xfe\xff")):
        return f"The file is not a text file, as a .{ext} file should be."
    return None


def check_upload(raw_filename: str | None, data: bytes) -> tuple[str, str]:
    """``(filename, content type)`` of a file a visitor may attach; :class:`UploadRefusedError` otherwise.

    The checks that cost nothing; :func:`inspect_upload` decodes what these
    only recognise.
    """
    filename = clean_filename(raw_filename)
    ext = _extension(filename)
    if ext not in ACCEPTED_EXTENSIONS:
        raise UploadRefusedError(
            415, f"Files of this type cannot be attached here. Accepted: {_accepted_list()}."
        )
    if not data:
        raise UploadRefusedError(400, "The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise UploadRefusedError(413, TOO_LARGE_DETAIL)
    problem = _content_problem(ext, data)
    if problem:
        raise UploadRefusedError(415, problem)
    return filename, guess_content_type(filename)


def _reader_refusal(exc: Any, detail: str) -> UploadRefusedError:
    """The refusal of a file the reader's worker did not finish with (``DocumentReadError``)."""
    if exc.retry_after is None:
        return UploadRefusedError(422, detail)
    return UploadRefusedError(503, READER_DOWN_DETAIL, retry_after=exc.retry_after)


async def inspect_upload(filename: str, data: bytes) -> None:
    """Decode what :func:`check_upload` only recognised, in the reader's worker process.

    ``widget_upload_inspection``: an image decoded as the format its name says,
    an Office file a bounded OOXML package. Raises :class:`UploadRefusedError`.
    """
    if not needs_inspection(filename):
        return
    from app.services.widget_document_reader import DocumentReadError, inspect_file

    try:
        problem = await inspect_file(filename, data)
    except DocumentReadError as exc:
        raise _reader_refusal(exc, UNCHECKABLE_DETAIL) from None
    if problem:
        raise UploadRefusedError(415, problem)


async def document_text(data: bytes, filename: str, content_type: str, *, file_id: uuid.UUID | str) -> str:
    """The text a visitor's document is to the model, whole: what the guard reads, what the message carries.

    The chat upload message's rule (``inline_attachment_text``, capped at
    :data:`INLINE_MAX_CHARS`), under the chat upload message's header
    (``file_resolver.upload_header``): the file named by its *file_id*, the
    visitor's name for it in an untrusted block. A widget turn has no
    tool that reads further, so a cut says so and nothing more. Read in the
    widget document reader's worker process; a document it could not read
    raises :class:`UploadRefusedError`.
    """
    from app.llm.file_resolver import upload_header
    from app.services.widget_document_reader import DocumentReadError, read_document

    try:
        text, total, stripped_total = await read_document(data, filename, content_type, INLINE_MAX_CHARS)
    except DocumentReadError as exc:
        raise _reader_refusal(exc, UNREADABLE_DETAIL) from None
    if stripped_total < 10:
        kind = _extension(filename).upper() or "FILE"
        return upload_header(str(file_id), filename, f" ({kind}, {len(data):,} bytes, no readable text)")
    shown = f"{upload_header(str(file_id), filename)}\n\n{text}"
    if total > INLINE_MAX_CHARS:
        shown += (
            f"\n\n[... truncated: the first {INLINE_MAX_CHARS:,} of {total:,} characters are "
            "shown. Answer from what is shown and say what you could not read.]"
        )
    return shown


# ---------------------------------------------------------------------------
# Storage quota
# ---------------------------------------------------------------------------


async def _usage(db: AsyncSession, agent_id: uuid.UUID, conversation_id: uuid.UUID) -> tuple[int, int, int]:
    """Files and bytes the session holds, and bytes the whole widget holds."""
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment

    in_session = FileAttachment.conversation_id == conversation_id
    row = (
        await db.execute(
            select(
                func.count(FileAttachment.id).filter(in_session),
                func.coalesce(func.sum(FileAttachment.size).filter(in_session), 0),
                func.coalesce(func.sum(FileAttachment.size), 0),
            )
            .select_from(FileAttachment)
            .join(Conversation, Conversation.id == FileAttachment.conversation_id)
            .where(
                FileAttachment.source == WIDGET_UPLOAD_SOURCE,
                FileAttachment.user_id.is_(None),
                Conversation.agent_id == agent_id,
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def _quota_refusal(usage: tuple[int, int, int], size: int) -> UploadRefusedError | None:
    files, session_bytes, widget_bytes = usage
    if files >= SESSION_MAX_FILES:
        return UploadRefusedError(
            413,
            f"This conversation already holds {SESSION_MAX_FILES} files, the most it can hold. "
            "Start a new conversation to attach more.",
        )
    if session_bytes + size > SESSION_MAX_BYTES:
        return UploadRefusedError(
            413,
            f"This file does not fit in the {_size(SESSION_MAX_BYTES)} a conversation can hold "
            f"({_size(session_bytes)} used). Start a new conversation to attach more.",
        )
    if widget_bytes + size > WIDGET_MAX_BYTES:
        return UploadRefusedError(507, "This assistant cannot store more files right now. Please try again later.")
    return None


async def check_room(agent_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Refuse an upload the session or the widget has no room left for, before its body is read."""
    from app.core.database import async_session

    async with async_session() as db:
        refused = _quota_refusal(await _usage(db, agent_id, conversation_id), 1)
    if refused is not None:
        raise refused


async def _lock_widget(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Serialize the quota check and the write of the uploads of one widget, for this transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:agent))"),
        {"scope": _LOCK_SCOPE, "agent": str(agent_id)},
    )


# ---------------------------------------------------------------------------
# Accepting an upload
# ---------------------------------------------------------------------------


async def _screen(
    *,
    agent: Any,
    conversation_id: uuid.UUID,
    filename: str,
    shown: str,
    audit_anchor_user_id: str | None,
) -> None:
    """The connected prompt guard's word on *shown*, the text the model will read of a document.

    Exactly :func:`document_text`, what the visitor's message carries.
    Audited like a widget message (``guard_chat_message``: as the guard,
    anchored on the admin the widget acts as), on a session of its own that
    commits the entry whatever happens to the upload next.
    """
    from app.services.prompt_guards import SURFACE_WIDGET_UPLOAD, guard_chat_message, known_unconnected

    if known_unconnected():
        return
    from app.core.database import async_session
    from app.core.security_options import security_option

    async with async_session() as db:
        refusal = await guard_chat_message(
            db,
            shown,
            user_id=None,
            entity_id=agent.id,
            entity_name=agent.name,
            details={
                "surface": SURFACE_WIDGET_UPLOAD,
                "chat_surface": "public_widget",
                "agent_id": str(agent.id),
                "agent_slug": agent.slug,
                "conversation_id": str(conversation_id),
                "channel": "web_embed",
                "filename": filename,
            },
            enforce=security_option("prompt_guard_block_public_widget"),
            audit_anchor_user_id=audit_anchor_user_id,
            surface=SURFACE_WIDGET_UPLOAD,
            timeout=GUARD_TIMEOUT_SECS,
        )
        await db.commit()
    if refusal:
        raise UploadRefusedError(422, GUARD_REFUSAL_DETAIL)


async def _remember_orphan(
    *, object_name: str, filename: str, content_type: str, size: int, sha256: str
) -> None:
    """Keep the key of an object whose row was not written and whose deletion failed.

    As a row with no conversation, which the retention sweep deletes - object
    first - within the hour. Nothing else reads such a row. Never raises: past
    a failure here the key is in the log line.
    """
    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment

    try:
        async with async_session() as db:
            db.add(
                FileAttachment(
                    user_id=None,
                    conversation_id=None,
                    filename=filename,
                    content_type=content_type,
                    size=size,
                    s3_object_name=object_name,
                    sha256=sha256,
                    source=WIDGET_UPLOAD_SOURCE,
                )
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "A public widget visitor file was left in storage with no row: %s (%s)",
            object_name,
            format_exception_message(exc),
        )


async def _store(
    *,
    file_id: uuid.UUID,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
) -> FileAttachment:
    """Write the object, then the row under the widget's lock; the object goes if the row does not."""
    from app.core.database import async_session
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment
    from app.services.file_cleanup import purge_storage_objects
    from app.services.file_storage import file_storage

    digest = hashlib.sha256(data).hexdigest()
    try:
        object_name = await file_storage.upload(
            data=data, filename=filename, content_type=content_type, prefix=f"widget/{conversation_id}"
        )
    except Exception as exc:
        logger.warning("Could not store a public widget visitor file: %s", format_exception_message(exc))
        raise UploadRefusedError(503, STORAGE_DOWN_DETAIL) from None
    try:
        async with async_session() as db:
            await _lock_widget(db, agent_id)
            refused = _quota_refusal(await _usage(db, agent_id, conversation_id), len(data))
            if refused is not None:
                raise refused
            row = FileAttachment(
                id=file_id,
                user_id=None,
                conversation_id=conversation_id,
                filename=filename,
                content_type=content_type,
                size=len(data),
                s3_object_name=object_name,
                sha256=digest,
                source=WIDGET_UPLOAD_SOURCE,
            )
            db.add(row)
            # An upload is activity of the session: its retention counts from here.
            await db.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(updated_at=datetime.now(UTC))
            )
            await db.commit()
    except BaseException:
        if not await purge_storage_objects([object_name]):
            await _remember_orphan(
                object_name=object_name, filename=filename, content_type=content_type, size=len(data), sha256=digest
            )
        raise
    return row


#: Stores whose visitor went away, kept alive until they end.
_abandoned: set[asyncio.Task] = set()


def _settle(task: asyncio.Task) -> None:
    _abandoned.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.debug("A public widget upload its visitor left ended with %s", repr(task.exception())[:200])


async def _finish_even_if_abandoned[T](work: Coroutine[Any, Any, T], permit: UploadPermit | None = None) -> T:
    """*work* in a task of its own, which a cancelled caller does not stop.

    A visitor who disconnects cancels the upload request; cancelled between the
    object store's put and the row's commit, the store would leave an object
    no row names, outside every quota and the retention. The task runs to its
    end instead: the row is written (an upload never sent, deleted by the
    retention a day later) or its failure path deletes the object. It keeps the
    caller's *permit* until then, so stores left running still count against
    :data:`MAX_CONCURRENT_UPLOADS`.
    """
    task = asyncio.ensure_future(work)
    _abandoned.add(task)
    task.add_done_callback(_settle)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if permit is not None and not task.done():
            permit.detached = True
            task.add_done_callback(lambda _task: permit.release())
        raise


async def accept_upload(
    *,
    agent: Any,
    conversation_id: uuid.UUID,
    raw_filename: str | None,
    data: bytes,
    audit_anchor_user_id: str | None,
    permit: UploadPermit | None = None,
) -> FileAttachment:
    """Check, read, screen and store a file a visitor attached to *conversation_id*.

    The quota is read with the file's size so a file that cannot fit is
    refused before anything decodes it, and read again under the widget's lock
    when the row is written. A document is read here, so one the reader cannot
    read is refused now rather than when the message is sent. No session is
    held while the guard evaluates. The store keeps the caller's upload
    *permit* if the caller leaves before it ends. Raises :class:`UploadRefusedError`.
    """
    from app.core.database import async_session

    filename, content_type = check_upload(raw_filename, data)
    async with async_session() as db:
        refused = _quota_refusal(await _usage(db, agent.id, conversation_id), len(data))
    if refused is not None:
        raise refused
    await inspect_upload(filename, data)
    # The row's id is chosen now: the text the guard screens names the file by it.
    file_id = uuid.uuid4()
    if not is_image(content_type):
        await _screen(
            agent=agent,
            conversation_id=conversation_id,
            filename=filename,
            shown=await document_text(data, filename, content_type, file_id=file_id),
            audit_anchor_user_id=audit_anchor_user_id,
        )
    return await _finish_even_if_abandoned(
        _store(
            file_id=file_id,
            agent_id=agent.id,
            conversation_id=conversation_id,
            filename=filename,
            content_type=content_type,
            data=data,
        ),
        permit,
    )


async def load_pending_uploads(
    db: AsyncSession, *, agent_id: uuid.UUID, conversation_id: uuid.UUID, file_ids: Sequence[str]
) -> list[FileAttachment]:
    """The uploads of this widget session *file_ids* name, not yet sent, in the given order.

    The session must be a widget session of *agent_id*: nothing of another
    widget's session is ever read. Raises :class:`UploadRefusedError` (400)
    when one of them is not such an upload: malformed, of another session,
    already sent with a message, or deleted.
    """
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment

    wanted: list[uuid.UUID] = []
    for raw in file_ids:
        file_uuid = safe_uuid(str(raw))
        if file_uuid is None:
            raise UploadRefusedError(400, FILE_GONE_DETAIL)
        if file_uuid not in wanted:
            wanted.append(file_uuid)
    if not wanted:
        return []
    rows = (
        await db.execute(
            select(FileAttachment)
            .join(Conversation, Conversation.id == FileAttachment.conversation_id)
            .where(
                FileAttachment.id.in_(wanted),
                FileAttachment.conversation_id == conversation_id,
                Conversation.agent_id == agent_id,
                Conversation.channel == "web_embed",
                FileAttachment.user_id.is_(None),
                FileAttachment.source == WIDGET_UPLOAD_SOURCE,
                FileAttachment.message_id.is_(None),
            )
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(wanted):
        raise UploadRefusedError(400, FILE_GONE_DETAIL)
    return [by_id[file_uuid] for file_uuid in wanted]


async def document_texts(rows: Sequence[FileAttachment]) -> dict[str, str]:
    """:func:`document_text` of each document among *rows*, by file id; images have none.

    Read before the visitor's message is stored, so no lock waits on the
    object store, and in one of the process's upload slots
    (:func:`upload_slot`), like the upload that stored them. Raises
    :class:`UploadRefusedError` when a file cannot be read.
    """
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    documents = [row for row in rows if not is_image(row.content_type)]
    if not documents:
        return {}
    texts: dict[str, str] = {}
    async with upload_slot():
        for row in documents:
            try:
                data = await file_storage.download(row.s3_object_name)
            except FileNotFoundInStorageError:
                raise UploadRefusedError(400, FILE_GONE_DETAIL) from None
            except Exception as exc:
                logger.warning("Could not read a public widget visitor file: %s", format_exception_message(exc))
                raise UploadRefusedError(503, STORAGE_DOWN_DETAIL) from None
            texts[str(row.id)] = await document_text(data, row.filename, row.content_type, file_id=row.id)
    return texts


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _expired_uploads_clause(now: datetime) -> Any:
    """The visitor uploads whose session is over, and the files of queued channel messages that never ran."""
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment
    from app.services.channel_continuation import expired_queued_files_clause

    idle_since = now - timedelta(days=max(1, settings.public_widget_upload_retention_days))
    widget_expired = and_(
        FileAttachment.source == WIDGET_UPLOAD_SOURCE,
        FileAttachment.user_id.is_(None),
        or_(
            FileAttachment.conversation_id.is_(None),
            and_(FileAttachment.message_id.is_(None), FileAttachment.created_at < now - UNSENT_RETENTION),
            Conversation.updated_at < idle_since,
        ),
    )
    return or_(widget_expired, expired_queued_files_clause(now))


async def _purge_batch(where: Any, batch: int) -> tuple[int, bool]:
    """Delete one batch of the files matching *where*; ``(rows deleted, whether the store answered every delete)``.

    Each object is deleted before its row, in the transaction that locks the
    rows: a store that does not answer stops the batch, and the rows whose
    object may still exist stay for the next sweep.
    """
    from app.core.database import async_session
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    async with async_session() as db:
        rows = (
            await db.execute(
                select(FileAttachment.id, FileAttachment.s3_object_name)
                .outerjoin(Conversation, Conversation.id == FileAttachment.conversation_id)
                .where(where)
                .order_by(FileAttachment.created_at)
                .limit(batch)
                .with_for_update(of=FileAttachment, skip_locked=True)
            )
        ).all()
        gone: list[uuid.UUID] = []
        answered = True
        for row_id, object_name in rows:
            try:
                if object_name:
                    await file_storage.delete(object_name)
            except Exception as exc:
                logger.warning(
                    "Object store unreachable while deleting expired unsent uploads (%s); "
                    "%d left for the next sweep",
                    type(exc).__name__,
                    len(rows) - len(gone),
                )
                answered = False
                break
            gone.append(row_id)
        if gone:
            await db.execute(
                delete(FileAttachment)
                .where(FileAttachment.id.in_(gone))
                .execution_options(synchronize_session=False)
            )
        await db.commit()
    return len(gone), answered


async def purge_expired_uploads(
    *, now: datetime | None = None, batch: int = PURGE_BATCH, max_batches: int = PURGE_MAX_BATCHES
) -> int:
    """Delete the visitor uploads whose session is over, and the files of queued channel messages that never ran; return how many were deleted.

    A file whose conversation was deleted (or whose row was never written, see
    :func:`_remember_orphan`), a file never sent after :data:`UNSENT_RETENTION`,
    and every file of a session with no message for
    ``PUBLIC_WIDGET_UPLOAD_RETENTION_DAYS``; a Slack or Teams file still
    waiting for its queued message past ``QUEUED_FILE_RETENTION``
    (``channel_continuation.expired_queued_files_clause``). Oldest first,
    *batch* at a time in a transaction each, at most *max_batches* a run. The
    rows are locked ``SKIP LOCKED``, so a message binding one of them, or a
    turn claiming it, at that moment keeps it.
    """
    removed = await purge_unsent_files(
        _expired_uploads_clause(now or datetime.now(UTC)), batch=batch, max_batches=max_batches
    )
    if removed:
        logger.info("Deleted %d unsent upload(s) past their retention", removed)
    return removed


async def purge_unsent_files(
    where: Any, *, batch: int = PURGE_BATCH, max_batches: int = PURGE_MAX_BATCHES
) -> int:
    """Delete the files matching *where*, each object before its row; return how many rows were deleted.

    *where* names files no message shows (``FileAttachment.message_id IS
    NULL`` among its terms). Oldest first, *batch* at a time in a transaction
    each, at most *max_batches* a run, the rows locked ``SKIP LOCKED``; a
    store that does not answer stops the run and leaves the rest as they are.
    Also deletes the files of a queued channel message that never runs
    (``channel_continuation.discard_queued_files``).
    """
    removed = 0
    for _ in range(max_batches):
        deleted, answered = await _purge_batch(where, batch)
        removed += deleted
        if deleted < batch or not answered:
            break
    return removed


async def purge_expired_uploads_if_due() -> int:
    """Run :func:`purge_expired_uploads` at most once per interval, install-wide.

    Asked on every scheduler tick; a Redis lease decides, like the other
    install-wide sweeps. A lease that cannot be taken skips this tick.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        if not await redis.set(_PURGE_LEASE_KEY, "1", nx=True, ex=PURGE_INTERVAL_SECS):
            return 0
    except Exception as exc:
        logger.debug("Widget upload purge lease not taken: %s", exc)
        return 0
    return await purge_expired_uploads()
