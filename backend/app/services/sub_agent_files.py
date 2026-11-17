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

"""Files a background sub-agent task produced, carried back to its parent.

A delegated task writes files the way any agent turn does (``generate_file``,
the code interpreter, a download tool): each one lands as a ``FileAttachment``
row and a ``FileRef`` in the ``FileContext`` of the CHILD loop. Left there, the
only trace of the files would be the ``[[FILE:<id>]]`` markers in the task's
text result, and the parent could neither read the files
(``read_file_content`` only resolves refs in ITS context) nor show them (the
chat renders a marker only when the message carries the file as an attachment).

This module is the one place that knows how a task's files travel:

* :func:`produced_file_ids` - what the child loop ADDED to the context it was
  handed (the inherited parent files are not the task's output);
* ``SubAgentTask.metadata_json[RESULT_FILE_IDS_KEY]`` - where the ids are
  recorded by ``_execute_sub_agent_task`` (read back with
  :func:`task_result_file_ids`);
* :func:`load_result_file_refs` - the ``FileRef`` list a tool result or a wake
  turn puts into the parent's ``FileContext``. The refs go INTO the context
  (the tool executor's, the wake turn's) and never onto ``ToolResult.files``
  or a steering message's ``files``: the loop copies those onto the transcript
  message, and every provider then inlines each non-native file's extracted
  text (up to 80,000 characters per file) into the next request;
* :func:`register_context_files` - the same registration for a parent wake
  that folded into a turn already running (``SteeringPayload.context_file_ids``,
  read user-scoped by the running turn's bridge);
* :func:`register_result_refs` - how a handed-back ref joins the parent's
  context: a deliverable as a file the parent's reply carries, a working file
  as a file to read, carried only when the reply names it;
  :func:`deliverable_rows` - the same split for a completion notice;
* :func:`format_result_files_section` - the per-turn text naming the files to
  the model (a tool result / wake prompt block, never system-prompt text);
* :func:`load_attachment_rows` / :func:`attachment_metadata` /
  :func:`bind_attachment_rows` / :func:`attach_rows_to_message` - the
  persistence half: the ``attachments`` block on the message that shows the
  files, and the row linkage (``conversation_id`` + ``message_id``) the
  message-scoped download URL requires. A child's files are born with
  ``conversation_id = NULL`` (the child runs without a conversation), so
  unlike the chat path the linkage here has to set BOTH halves.

A ``FileAttachment`` row is shown by AT MOST ONE message. The chip on a message
downloads through ``/conversations/{cid}/messages/{mid}/files/{fid}/download``,
which matches the row on BOTH ids, so re-pointing a row at a second message
breaks the chip that already shows it without making the new one work. The
same task can be read from another conversation of the same user (or again in
its own once the history window dropped the message), so the admission rule is
enforced in two places that must agree: :func:`load_attachment_rows` admits a
row only when it is not yet shown (``message_id IS NULL``) and is unbound or
bound to THIS conversation, and :func:`bind_attachment_rows` repeats that
predicate in its UPDATE so two concurrent handoffs cannot both bind the same
row - the loser's UPDATE matches nothing, and the caller writes the
``attachments`` block from the rows that were actually bound.

The recorded ids are platform-written (never model-supplied) and read off a
task row the caller already loaded user-scoped, so the loaders here filter by
id only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select, update

from app.core.utils import format_exception_message, safe_uuid
from app.tools.base import FileContext, files_for_reply, register_fetched_file

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

logger = logging.getLogger(__name__)

#: Upper bound on waiting for an in-flight incremental record write
#: (:meth:`RecordingFileContext.drain`) before a terminal write takes over.
RECORD_FLUSH_DRAIN_SECS = 5.0

#: ``SubAgentTask.metadata_json`` key holding the ids of the files the task
#: produced, in the order the child loop registered them.
RESULT_FILE_IDS_KEY = "result_file_ids"

#: ``extra_metadata`` key a wait-budget continuation is spawned with: the files
#: the INCOMPLETE task it continues had already produced. The continuation
#: inherits them in its ``FileContext`` (so it can read them), which is exactly
#: why :func:`produced_file_ids` would not count them - this carries them into
#: the continuation's own record so the task the parent finally wakes on names
#: every file the delegation produced.
CARRIED_FILE_IDS_KEY = "carried_result_file_ids"

#: ``extra_metadata`` key of a task whose prompt comes with attached files
#: (``run_agent_once`` with ``file_ids``): the ids, among the files the task is
#: handed in its ``FileContext``, that ride on the run's user message the way a
#: chat message's attachments do (images as vision blocks), not merely readable.
TURN_FILE_IDS_KEY = "turn_file_ids"

#: At most this many files ride on one task's message: the web chat's
#: ``MAX_CHAT_ATTACHMENTS_PER_MESSAGE`` (frontend/src/lib/constants.ts).
MAX_TURN_FILES = 30

#: Bound on what one task records. A run that writes more files than this is
#: not a deliverable anyone consolidates by hand: the first ``MAX_RESULT_FILES``
#: (in the order the loop registered them) are handed over, the rest stay in
#: storage under the task's user but are NOT reachable through the handoff -
#: a ``[[FILE:id]]`` marker with no attachment behind it renders nothing. The
#: truncation is logged (:func:`produced_file_ids`) so a run that hits it is
#: visible to an operator.
MAX_RESULT_FILES = 50


def _dedupe(ids: Iterable[object]) -> list[str]:
    """Valid UUID strings from *ids*, first occurrence wins, order kept."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        if not isinstance(raw, str):
            continue
        parsed = safe_uuid(raw.strip())
        if parsed is None:
            continue
        key = str(parsed)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def produced_file_ids(
    file_context: object | None, inherited_ids: Iterable[str], answer: str | None = None
) -> list[str]:
    """Ids the run ADDED to *file_context* beyond *inherited_ids*, in insertion order.

    *inherited_ids* is the snapshot taken before the loop ran (the parent's
    working files the task was handed); everything else in the context when
    the loop returns is the task's output - except a file the run only fetched
    to read, which is handed over when *answer* names it. Bounded by
    :data:`MAX_RESULT_FILES`.
    """
    if file_context is None or not hasattr(file_context, "list_files"):
        return []
    inherited = {str(i) for i in inherited_ids}
    produced = _dedupe(
        files_for_reply(
            file_context,
            [
                ref.file_id
                for ref in file_context.list_files()
                if getattr(ref, "file_id", None) and str(ref.file_id) not in inherited
            ],
            answer,
        )
    )
    if len(produced) > MAX_RESULT_FILES:
        logger.warning(
            "A background task produced %d files; only the first %d are handed "
            "to its parent (task %s)",
            len(produced),
            MAX_RESULT_FILES,
            getattr(file_context, "task_id", "?"),
        )
    return produced[:MAX_RESULT_FILES]


def task_turn_file_refs(file_context: object | None, task_or_metadata: object) -> list[FileRef]:
    """The refs of *file_context* the task's :data:`TURN_FILE_IDS_KEY` names, in that order.

    Empty when the task names none (every delegation but ``run_agent_once``
    with files) or the context does not hold them.
    """
    wanted = task_result_file_ids(task_or_metadata, key=TURN_FILE_IDS_KEY)
    if not wanted or file_context is None or not hasattr(file_context, "list_files"):
        return []
    held = {str(getattr(ref, "file_id", "")): ref for ref in file_context.list_files()}
    return [held[file_id] for file_id in wanted if file_id in held][:MAX_TURN_FILES]


def task_result_file_ids(
    task_or_metadata: object, *, key: str = RESULT_FILE_IDS_KEY
) -> list[str]:
    """The recorded produced-file ids of a task row (or its metadata dict).

    *key* selects the list to read (:data:`RESULT_FILE_IDS_KEY` by default,
    :data:`CARRIED_FILE_IDS_KEY` for a continuation's inherited record).
    Tolerant of every malformed shape a JSON column can hold: a missing key,
    a non-list, non-string entries and non-UUID strings all read as "no file",
    never as an error - the caller is a tool result or a wake turn.
    """
    if isinstance(task_or_metadata, dict):
        meta: object = task_or_metadata
    else:
        meta = getattr(task_or_metadata, "metadata_json", None)
    if not isinstance(meta, dict):
        return []
    raw = meta.get(key)
    if not isinstance(raw, list):
        return []
    return _dedupe(raw)


def merge_result_file_ids(
    carried_ids: Iterable[str], produced_ids: Iterable[str]
) -> list[str]:
    """The record of a task that continues another: carried first, then its own.

    *carried_ids* are the predecessor's files (:data:`CARRIED_FILE_IDS_KEY`),
    *produced_ids* what this run added. Deduplicated, order kept, and bounded
    by :data:`MAX_RESULT_FILES` AFTER the merge - the bound is on what one task
    records, whichever attempt wrote the file.
    """
    return _dedupe([*carried_ids, *produced_ids])[:MAX_RESULT_FILES]


# ``metadata_json`` keys: the latest orchestration attempt that started the
# task, written by an attempt the engine started to replace another,
# and its epoch, which a hold the engine defers opens. The attempt
# stays a bare number, as the previous release reads it; a row without the
# epoch key is epoch 0.
TASK_ATTEMPT_KEY = "orchestration_attempt"
TASK_ATTEMPT_EPOCH_KEY = "orchestration_attempt_epoch"


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def task_attempt_fence(metadata: object) -> tuple[int, int]:
    """``(epoch, attempt)`` of the latest attempt *metadata* records as
    started; ``(0, 0)`` when none is."""
    if not isinstance(metadata, dict):
        return 0, 0
    return (
        _count(metadata.get(TASK_ATTEMPT_EPOCH_KEY)) or 0,
        _count(metadata.get(TASK_ATTEMPT_KEY)) or 0,
    )


def task_attempt_place(metadata: object, attempt: int | None) -> tuple[int, int] | None:
    """This attempt's place, from the task as it read it at its start:
    ``None`` for a task not run by the orchestration engine."""
    if attempt is None:
        return None
    return task_attempt_fence(metadata)[0], attempt


def with_task_attempt_fence(metadata: object, place: tuple[int, int]) -> dict:
    base = dict(metadata) if isinstance(metadata, dict) else {}
    return {**base, TASK_ATTEMPT_EPOCH_KEY: place[0], TASK_ATTEMPT_KEY: place[1]}


def task_fence_for_deferral(metadata: object) -> dict | None:
    """*metadata* with the next epoch opened, for a live attempt whose hold the
    engine defers, or ``None`` when nothing needs to change - the task
    counterpart of ``attempt_charges.fence_for_deferral``: a deferral restores
    the attempt count, so the next claim may carry a lower number than a
    re-run that fenced the task meanwhile."""
    epoch, attempt = task_attempt_fence(metadata)
    if attempt == 0:
        return None
    return with_task_attempt_fence(metadata, (epoch + 1, 0))


async def write_task_result_file_ids(
    task_id: str, file_ids: Sequence[str], *, place: tuple[int, int] | None = None
) -> bool:
    """Write *file_ids* as the produced-file record of task *task_id*.

    On its own short-lived session, ``FOR UPDATE`` so it serializes with the
    other writers of the row (the task body's terminal write, the adapter's
    ``on_dead_letter``, a kill). Only :data:`RESULT_FILE_IDS_KEY` changes; the
    rest of ``metadata_json`` is carried over as read under the lock. Returns
    ``True`` when the row was written, ``False`` when it already held the
    same record, does not exist, has ended, or was started again by a later
    attempt than the one at *place* (``task_attempt_place``): the files on the row are
    those of the ending that stands or of the attempt that replaced this one,
    and an attempt whose lease was lost never replaces them. Raises on
    a database failure - the caller decides what a failed write costs.
    """
    from app.core.database import async_session
    from app.models.sub_agent_task import SubAgentTask
    from app.tools.wait_policy import WAIT_TERMINAL_STATUSES

    task_uuid = safe_uuid(task_id)
    if task_uuid is None:
        return False
    ids = _dedupe(file_ids)[:MAX_RESULT_FILES]
    async with async_session() as db:
        row = (
            await db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.id == task_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or getattr(row, "status", None) in WAIT_TERMINAL_STATUSES:
            return False
        raw_meta = row.metadata_json
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        if place is not None and task_attempt_fence(meta) > place:
            return False
        if task_result_file_ids(meta) == ids:
            return False
        row.metadata_json = {**meta, RESULT_FILE_IDS_KEY: ids}
        await db.commit()
        return True


class RecordingFileContext(FileContext):
    """The child task's FileContext: records what the loop adds AS it adds it.

    The terminal write in ``_execute_sub_agent_task`` runs when the run reaches
    one of its in-process ends - the result, a timeout, an exception. A worker
    can also be killed, or lose its lease, AFTER the loop wrote files and
    BEFORE it returns; the orchestration engine then dead-letters the task from
    another process (``SubAgentAdapter.on_dead_letter``), which flips it to
    ``failed`` and wakes the parent - and has no FileContext to read, so the
    parent would learn of the failure but never of the batches already
    written. So every file the loop adds beyond the inherited set is written
    to the task row right away (:func:`write_task_result_file_ids`, own session,
    best effort - a failed write is logged and the terminal write repeats it).
    Writes coalesce: one flush in flight at a time, re-run once when adds
    landed meanwhile, always writing the CURRENT full record (carried ids
    first, then produced, bounded after the merge). :meth:`drain` awaits the
    in-flight flush so the terminal write is the last writer on the
    in-process paths.
    """

    def __init__(
        self,
        *,
        task_id: str,
        carried_ids: Iterable[str] = (),
        inherited: Iterable[FileRef] = (),
        place: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.task_id = task_id
        #: The place of the orchestration attempt writing the record.
        self.place = place
        self.carried_ids: list[str] = _dedupe(carried_ids)
        for ref in inherited:
            super().add(ref)
        #: The parent's working files the task was handed; never "produced".
        self.inherited_ids: frozenset[str] = frozenset(
            str(ref.file_id) for ref in self.list_files()
        )
        self._dirty = False
        self._flush: asyncio.Task | None = None

    @classmethod
    def from_parent(
        cls,
        file_context: object | None,
        *,
        task_id: str,
        carried_ids: Iterable[str] = (),
        place: tuple[int, int] | None = None,
        resumed: Iterable[FileRef] = (),
    ) -> RecordingFileContext:
        """Wrap the context the task was handed (or none) into a recording one.

        *resumed* are the files the attempts before this one produced
        (:func:`resumed_file_refs`): readable again by their ids, and
        inherited, so they are not counted as this attempt's output (the
        carried record already names the ones they recorded).
        """
        refs = (
            list(file_context.list_files())
            if file_context is not None and hasattr(file_context, "list_files")
            else []
        )
        known = {str(getattr(ref, "file_id", "")) for ref in refs}
        refs.extend(ref for ref in resumed if str(getattr(ref, "file_id", "")) not in known)
        return cls(task_id=task_id, carried_ids=carried_ids, inherited=refs, place=place)

    def record(self) -> list[str]:
        """The task's produced-file record as it stands now."""
        return merge_result_file_ids(
            self.carried_ids, produced_file_ids(self, self.inherited_ids)
        )

    def add(self, file_ref: FileRef) -> None:
        super().add(file_ref)
        file_id = str(getattr(file_ref, "file_id", ""))
        # A fetched file joins the record only when the answer names it.
        if file_id in self.inherited_ids or self.is_fetched(file_id):
            return
        self._dirty = True
        if self._flush is not None and not self._flush.done():
            return  # the running flush re-reads the record before it exits
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to flush on: the terminal write is the record
        self._flush = loop.create_task(self._flush_record())

    async def _flush_record(self) -> None:
        while self._dirty:
            self._dirty = False
            ids = self.record()
            if not ids:
                continue
            try:
                await write_task_result_file_ids(self.task_id, ids, place=self.place)
            except Exception as exc:
                logger.warning(
                    "Could not record the %d file(s) task %s produced so far "
                    "(the terminal write will repeat it): %s",
                    len(ids),
                    self.task_id,
                    format_exception_message(exc),
                )
                return

    async def drain(self, timeout: float = RECORD_FLUSH_DRAIN_SECS) -> None:
        """Wait (bounded) for an in-flight incremental write to finish.

        Called before a terminal write so the two never interleave; never
        raises - a flush still running at the bound is left to finish on its
        own, and the terminal write that follows repeats its content anyway.
        """
        flush = self._flush
        if flush is None or flush.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(flush), timeout)
        except Exception:
            logger.debug(
                "Incremental file record of task %s still in flight at drain",
                self.task_id,
            )


def attachable_filter(conversation_id: uuid.UUID):
    """SQL predicate: rows that may be attached to a message of *conversation_id*.

    Not yet shown by any message (``message_id IS NULL``) and either unbound or
    already bound to this conversation. The ONE definition shared by the
    admission read (:func:`load_attachment_rows`) and the atomic bind
    (:func:`bind_attachment_rows`), so the two cannot drift.
    """
    from app.models.file_attachment import FileAttachment

    return and_(
        FileAttachment.message_id.is_(None),
        or_(
            FileAttachment.conversation_id.is_(None),
            FileAttachment.conversation_id == conversation_id,
        ),
    )


async def load_attachment_rows(
    file_ids: Sequence[str],
    *,
    db: AsyncSession | None = None,
    conversation_id: object | None = None,
    user_id: object | None = None,
) -> list[FileAttachment]:
    """``FileAttachment`` rows for *file_ids*, in the order the ids were given.

    With *conversation_id*, only rows :func:`attachable_filter` admits for that
    conversation are returned: a row bound to another conversation must not be
    re-homed (its message-scoped download URL would break there) and must not
    be attached to a message here (the URL would 404 here); a row already shown
    by a message - in this conversation or another - keeps that message. An
    unparsable *conversation_id* admits nothing (fail closed). Callers that
    only need the refs pass no conversation.

    With *user_id*, only rows that user owns are returned - for ids that did
    NOT come off a task row the caller loaded user-scoped, i.e. the ids a
    steering inbox entry carries (:func:`register_context_files`). An
    unparsable *user_id* admits nothing.

    Uses *db* when given; otherwise a dedicated short-lived session, so a tool
    running concurrently under ``asyncio.gather`` never touches the shared chat
    session, and a read that fails never poisons the session a caller is about
    to persist a message on.
    """
    from app.models.file_attachment import FileAttachment

    ids = _dedupe(file_ids)
    if not ids:
        return []
    parsed = [uuid.UUID(i) for i in ids]

    stmt = select(FileAttachment).where(FileAttachment.id.in_(parsed))
    if conversation_id is not None:
        conv_uuid = safe_uuid(str(conversation_id))
        if conv_uuid is None:
            return []
        stmt = stmt.where(attachable_filter(conv_uuid))
    if user_id is not None:
        user_uuid = safe_uuid(str(user_id))
        if user_uuid is None:
            return []
        stmt = stmt.where(FileAttachment.user_id == user_uuid)

    async def _run(session: AsyncSession) -> list[FileAttachment]:
        rows = (await session.execute(stmt)).scalars().all()
        by_id = {str(row.id): row for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    if db is not None:
        return await _run(db)
    from app.core.database import async_session

    async with async_session() as own_db:
        return await _run(own_db)


async def load_result_file_refs(
    file_ids: Sequence[str],
    *,
    db: AsyncSession | None = None,
    user_id: object | None = None,
) -> list[FileRef]:
    """``FileRef`` list for *file_ids* (rows that no longer exist are skipped).

    *user_id* narrows the read to that user's rows (see
    :func:`load_attachment_rows`). Never raises: a file lookup that fails
    costs the refs, not the tool result or the wake turn that asked for them.
    """
    from app.llm.base import FileRef

    if not file_ids:
        return []
    try:
        rows = await load_attachment_rows(file_ids, db=db, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "Could not load the files of a background task result: %s",
            format_exception_message(exc),
        )
        return []
    return [FileRef.from_attachment(row) for row in rows]


async def register_context_files(
    file_context: object | None,
    file_ids: Sequence[str],
    *,
    user_id: object,
) -> list[FileRef]:
    """Put the refs behind *file_ids* into *file_context* - and nowhere else.

    The bridge half of ``SteeringPayload.context_file_ids``: a parent
    wake that found the conversation's turn already running folds the finished
    task's files here, and the running loop's bridge registers them so the
    ``read_file_content`` the wake text asks for resolves. The refs are NOT
    put on the steering message: every provider inlines a user message's
    files (up to 80,000 extracted characters each), and the wake text already
    names them. Read user-scoped - the inbox is keyed by conversation, and an
    id in it is trusted no further than the user the turn runs as. Never
    raises; returns the refs registered (empty without a context).
    """
    if file_context is None or not hasattr(file_context, "add") or not file_ids:
        return []
    refs = await load_result_file_refs(file_ids, user_id=user_id)
    register_result_refs(file_context, refs)
    return refs


async def resumed_file_refs(
    carried_ids: Iterable[str], history: Sequence[object], *, user_id: object
) -> list[FileRef]:
    """The files a resumed task attempt must read again, as ``FileRef``.

    A task its worker handed back (graceful stop) or lost (lease loss) runs
    again from its checkpoint, which keeps each replayed result's text, never
    the files it carried. The new attempt's context is rebuilt from the
    parent's files only, so a contact sheet the lost attempt wrote answered
    "no file attached". This loads, with the same ids: the files the lost
    attempts recorded (*carried_ids*, :data:`CARRIED_FILE_IDS_KEY`) and every
    file a replayed result in *history* names
    (``assignment_run_resume.replayed_file_ids``, as a run's resume does), read as *user_id*'s rows only, never a template asset. A row
    retention purged is not found and stays unreadable. Never raises: a failed
    lookup costs the files, never the resume.
    """
    from app.llm.base import FileRef
    from app.services.assignment_run_resume import MAX_REPLAYED_FILE_IDS, replayed_file_ids

    ids = _dedupe([*carried_ids, *replayed_file_ids(list(history))])[:MAX_REPLAYED_FILE_IDS]
    if not ids:
        return []
    try:
        rows = await load_attachment_rows(ids, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "Could not load the files a resumed background task produced before: %s",
            format_exception_message(exc),
        )
        return []
    return [
        FileRef.from_attachment(row) for row in rows if getattr(row, "template_container_id", None) is None
    ]


def register_result_refs(file_context: object | None, refs: Iterable[FileRef]) -> None:
    """Put a task's handed-back *refs* into the parent's *file_context*.

    A deliverable joins it like a file the parent wrote: its reply carries it.
    A working file (a check's picture, a scratch file: ``is_working_file``)
    joins it as a file fetched to READ (``register_fetched_file``): the
    parent opens it by id with ``read_file_content``, and its reply carries it
    only when the text names it - a QA sweep's forty contact sheets are the
    task's working material, not files for the person.
    """
    if file_context is None or not hasattr(file_context, "add"):
        return
    for ref in refs:
        if getattr(ref, "is_working_file", False):
            register_fetched_file(file_context, ref)
        else:
            file_context.add(ref)


def deliverable_rows(rows: Iterable[FileAttachment], text: str | None = None) -> list[FileAttachment]:
    """*rows* without the working files *text* does not name: what a completion notice shows as files.

    A working file a task handed back stays readable by id from the parent's
    context (:func:`register_result_refs`); it is a file of the notice only
    when the notice's text names it (its ``[[FILE:id]]`` marker, its id), the
    rule :func:`~app.tools.base.files_for_reply` applies to a fetched file.
    """
    from app.llm.base import is_working_file_source

    named = (text or "").lower()
    return [
        row
        for row in rows
        if not is_working_file_source(getattr(row, "source", None)) or str(getattr(row, "id", "")).lower() in named
    ]


def attachment_metadata(rows: Iterable[FileAttachment]) -> list[dict]:
    """The ``attachments`` block of a message showing *rows* (chip shape)."""
    from app.services.chat_messages import attachment_meta_from_file_attachment

    return [attachment_meta_from_file_attachment(row) for row in rows]


async def bind_attachment_rows(
    db: AsyncSession,
    rows: Iterable[FileAttachment],
    *,
    conversation_id: object,
    message_id: object,
) -> list[FileAttachment]:
    """Link *rows* to the message that shows them; return the rows that were.

    Sets BOTH ``conversation_id`` and ``message_id``: the message-scoped
    download URL (``/conversations/{cid}/messages/{mid}/files/{fid}/download``)
    matches on both, and a sub-agent's file starts with neither.

    The UPDATE repeats :func:`attachable_filter`, so the admission check the
    caller did through :func:`load_attachment_rows` and the bind are ONE
    atomic step: two handoffs of the same task from two conversations (or a
    turn racing the completion notice) both load the unbound row, but only one
    UPDATE matches it - the other binds nothing and must not show the row. The
    returned list (given order, only the rows actually bound) is what the
    caller writes into the message's ``attachments`` block; a row that lost
    the race is logged and left where it is. Does not commit.
    """
    from app.models.file_attachment import FileAttachment

    given = [row for row in rows if getattr(row, "id", None) is not None]
    if not given:
        return []
    conv_uuid = safe_uuid(str(conversation_id))
    msg_uuid = safe_uuid(str(message_id))
    if conv_uuid is None or msg_uuid is None:
        return []
    result = await db.execute(
        update(FileAttachment)
        .where(FileAttachment.id.in_([row.id for row in given]))
        .where(attachable_filter(conv_uuid))
        .values(conversation_id=conv_uuid, message_id=msg_uuid)
        .returning(FileAttachment.id)
    )
    bound_ids = {str(row_id) for row_id in result.scalars().all()}
    bound = [row for row in given if str(row.id) in bound_ids]
    if len(bound) != len(given):
        logger.warning(
            "%d of %d file(s) were not bound to message %s of conversation %s: "
            "another message shows them already (left on that message)",
            len(given) - len(bound),
            len(given),
            msg_uuid,
            conv_uuid,
        )
    return bound


async def attach_rows_to_message(
    db: AsyncSession,
    message: object,
    rows: Sequence[FileAttachment],
    *,
    conversation_id: object,
) -> list[FileAttachment]:
    """Bind *rows* to *message* and write its ``attachments`` block from the
    rows that were actually bound.

    The order matters: ``store_message`` flushes (the message has an id) but
    does not commit, so the bind runs first and the block is written after it,
    on the same still-open transaction, from :func:`bind_attachment_rows`'s
    answer - a chip is never written for a row another message shows. The
    bind runs in a SAVEPOINT so a failed UPDATE costs the chips, not the
    message the caller is about to commit. Never raises; returns the rows
    bound (empty when nothing was).
    """
    if not rows:
        return []
    message_id = getattr(message, "id", None)
    if message_id is None:
        return []
    try:
        async with db.begin_nested():
            bound = await bind_attachment_rows(
                db, rows, conversation_id=conversation_id, message_id=message_id
            )
    except Exception as exc:
        logger.exception(
            "Failed to link %d file(s) to message %s of conversation %s; "
            "the message is persisted without them: %s",
            len(rows),
            message_id,
            conversation_id,
            format_exception_message(exc),
        )
        return []
    if not bound:
        return []
    meta: dict = {}
    raw_meta = getattr(message, "metadata_json", None)
    if isinstance(raw_meta, str) and raw_meta:
        try:
            parsed = json.loads(raw_meta)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    meta["attachments"] = attachment_metadata(bound)
    message.metadata_json = json.dumps(meta)  # type: ignore[attr-defined]
    return bound


def _human_size(size: object) -> str:
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


#: Bound on a filename / content type quoted into model-facing text.
_FILE_LINE_FIELD_CHARS = 200

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def _quoted_field(value: object, *, limit: int = _FILE_LINE_FIELD_CHARS) -> str:
    """*value* as ONE quoted token safe to place inside model-facing text.

    A filename or content type is user- or integration-controlled: one that
    carries a line break followed by an instruction would otherwise add a
    convincing platform-like line to the parent's tool result or wake prompt.
    Control characters (newlines included) become a space, the quote
    delimiter is escaped so the token cannot close itself early, and the
    field is bounded.
    """
    text = _CONTROL_CHARS_RE.sub(" ", str(value or "")).strip()
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return f'"{text}"'


def format_file_line(ref: object) -> str:
    """One ``- file_id="..." filename="..." (type, size)`` line for the model.

    The id is re-parsed as a UUID (anything else reads as an empty id) and the
    filename / content type go through :func:`_quoted_field`: the line is
    UNTRUSTED file metadata quoted into model-facing text, never a place an
    attachment name can write an instruction.
    """
    file_id = safe_uuid(str(getattr(ref, "file_id", "") or ""))
    return (
        f'- file_id="{file_id or ""}" '
        f"filename={_quoted_field(getattr(ref, 'filename', ''))} "
        f"({_quoted_field(getattr(ref, 'content_type', None) or 'application/octet-stream')}, "
        f"{_human_size(getattr(ref, 'size', 0))})"
    )


def format_result_files_section(
    refs: Sequence[object],
    *,
    heading: str = "Files produced by this task",
) -> str:
    """The text block that names a task's files to the parent model.

    Per-turn text riding on a tool result or the wake prompt - not a system
    prompt addition. It says the two things the model otherwise gets wrong: the
    content is NOT in its context (read it with ``read_file_content``, never
    reconstruct it), and the marker is what makes the file downloadable for the
    user. Empty when there is nothing to list.
    """
    if not refs:
        return ""
    lines = [
        f"{heading} ({len(refs)}) - now attached to your context. Their content "
        "is NOT in front of you: read a file with read_file_content(file_id) "
        "before quoting, merging or checking it; never reconstruct it from "
        "memory. To hand a file to the user, include its [[FILE:id]] "
        "marker in your reply."
    ]
    lines.extend(format_file_line(ref) for ref in refs)
    return "\n".join(lines)
