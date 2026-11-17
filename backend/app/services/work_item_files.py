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

"""The files a work item carries: attached by its creator, read by its readers.

A work item's description is prose; the files it is about stay where they were
produced - a chat upload, a generated file, a run's output - and the item names
them by id (``WorkItem.attachment_file_ids``). Nothing is copied: no byte goes
into the description, and the row keeps its owner and its binding.

Two halves, one rule each:

* **Attaching** (:func:`resolve_attachable_files`): a stored file (a
  ``file_attachments`` row, never a template asset) the creating turn holds -
  in its ``FileContext`` or bound to its conversation or run, the files its
  own ``read_file_content`` opens - or one its acting user owns: uploaded by
  the user, in one of the user's conversations, or produced by one of the
  user's runs (the Files page's "Mine" segment). An event run thus attaches a
  file another conversation of the same user produced, by its id or by an 8+
  hexadecimal prefix of it (:func:`resolve_owned_file_id`; a prefix that
  starts several of the user's files is refused with the candidates). Anything else - a file
  of another user above all - is refused by name before the item exists, so
  an id typed into the call never widens what the user may read.
  ``create_work_item`` and ``complete_work_item`` write the column;
  the completion also attaches the files its result cites
  (:func:`cited_file_ids`), under the same rule, warning on the ones refused.
* **Reading** (:func:`work_item_file_refs` + :func:`register_work_item_files`):
  the reader of a visible item gets the refs of the rows still stored,
  registered in its turn's ``FileContext`` as files it fetched to read, so
  ``read_file_content`` resolves them whatever conversation the reader runs
  in. The item's visibility is the grant - whoever reads the description reads
  the files it is about - and the run finalizer never re-homes a row another
  conversation or run holds (``assignment_engine._link_run_output_files``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, select

from app.core.utils import format_exception_message, safe_uuid

if TYPE_CHECKING:
    from app.llm.base import FileRef
    from app.tools.base import FileContext

logger = logging.getLogger(__name__)

#: Files one create_work_item or complete_work_item call may attach: one
#: round of evidence (a brief and its sources, a review's captures).
MAX_WORK_ITEM_ATTACHMENTS_PER_CALL = 10

#: Files one work item may carry over its whole life: several rounds of
#: evidence (a UX item reopened round after round attaches each round's
#: captures), not a file store. Every one stays readable by its readers.
MAX_WORK_ITEM_ATTACHMENTS = 60

#: Files the inbox run an item is dispatched to gets inlined on its first
#: message: the newest ones, the latest round. The older ones are read by
#: reference, get_work_item registers every attached file for the reader.
MAX_DISPATCH_INLINED_ATTACHMENTS = 10


def attachment_cap_refusal(carried: int, added: int, action: str) -> str:
    """The refusal past :data:`MAX_WORK_ITEM_ATTACHMENTS`, naming the cap and the count."""
    return (
        f"a work item carries at most {MAX_WORK_ITEM_ATTACHMENTS} attached files over its life: "
        f"it carries {carried} and this call adds {added} new ({carried + added}). "
        f"Nothing was {action}; attach the files the readers need."
    )


@dataclass
class AttachableFiles:
    """What :func:`resolve_attachable_files` admitted and refused."""

    refs: list[FileRef] = field(default_factory=list)
    #: The ids as the caller spelled them, in the order given.
    refused: list[str] = field(default_factory=list)
    #: The refusal of each id prefix that starts several of the user's files, by the id as spelled.
    ambiguous: dict[str, str] = field(default_factory=dict)

    def refusal_reason(self) -> str:
        """Why the refused ids were refused: the candidates of an ambiguous prefix, else ``""``."""
        return " ".join(self.ambiguous[raw] for raw in self.refused if raw in self.ambiguous)

    @property
    def file_ids(self) -> list[str]:
        return [ref.file_id for ref in self.refs]


def _row_uuid(file_id: object) -> uuid.UUID | None:
    return safe_uuid(str(file_id or "").strip().removeprefix("file-"))


async def _stored_rows(ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, object]:
    """The ``file_attachments`` rows behind *ids* that are not template assets."""
    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment

    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    async with async_session() as db:
        rows = (
            await db.execute(
                select(FileAttachment).where(
                    FileAttachment.id.in_(wanted),
                    FileAttachment.template_container_id.is_(None),
                )
            )
        ).scalars().all()
    return {row.id: row for row in rows}


async def _own_row_ids(ids: Iterable[uuid.UUID], user_id: object) -> set[uuid.UUID]:
    """The ids of *ids* whose ``file_attachments`` row *user_id* owns.

    Owned is the Files page's "Mine" predicate (``api.v1.files.mine_files_filter``):
    uploaded by the user, in one of the user's conversations, or produced by one
    of the user's runs; every term NULL-guarded. Template assets are left out.
    """
    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment

    owner = safe_uuid(str(user_id or ""))
    wanted = list(dict.fromkeys(ids))
    if owner is None or not wanted:
        return set()
    mine = _mine_clause(owner)
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(FileAttachment.id).where(
                        FileAttachment.id.in_(wanted),
                        FileAttachment.template_container_id.is_(None),
                        mine,
                    )
                )
            )
            .scalars()
            .all()
        )
    return set(rows)


def _mine_clause(owner: uuid.UUID) -> ColumnElement[bool]:
    """A ``file_attachments`` row *owner* owns: the Files page's "Mine" predicate
    (uploaded by the user, in one of the user's conversations, or produced by one of
    the user's runs), every term NULL-guarded."""
    from sqlalchemy import and_, or_

    from app.models.assignment import AssignmentRun
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment

    return or_(
        and_(FileAttachment.user_id.is_not(None), FileAttachment.user_id == owner),
        and_(
            FileAttachment.conversation_id.is_not(None),
            FileAttachment.conversation_id.in_(select(Conversation.id).where(Conversation.user_id == owner)),
        ),
        and_(
            FileAttachment.assignment_run_id.is_not(None),
            FileAttachment.assignment_run_id.in_(select(AssignmentRun.id).where(AssignmentRun.user_id == owner)),
        ),
    )


async def _own_rows_by_prefix(prefix: str, user_id: object) -> list[tuple[uuid.UUID, str]]:
    """The ``(id, filename)`` of the stored files *user_id* owns whose id starts with
    *prefix* (as ``id_prefix.id_prefix`` writes it), at most ``MAX_CANDIDATES`` + 1.

    The same ownership as :func:`_own_row_ids`, so a prefix never reaches another
    user's file. Template assets are left out.
    """
    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment
    from app.services.builtin_tools.id_prefix import ids_by_prefix

    owner = safe_uuid(str(user_id or ""))
    if owner is None:
        return []
    async with async_session() as db:
        return await ids_by_prefix(
            db,
            FileAttachment,
            prefix,
            (FileAttachment.template_container_id.is_(None), _mine_clause(owner)),
            label=FileAttachment.filename,
        )


async def resolve_owned_file_id(file_id: object, user_id: object) -> tuple[uuid.UUID | None, str | None]:
    """The id of the one stored file *user_id* owns that *file_id* names: its full id
    (bare, ``file-<id>``, the ``[[FILE:<id>]]`` marker) or an 8+ hexadecimal prefix of it.

    Returns ``(id, None)`` for one file, ``(None, None)`` when the user owns none (a file of
    another user answers the same, so the answer never confirms it exists) and
    ``(None, refusal)`` naming the candidates when a prefix starts several. Never raises.
    """
    from app.services.builtin_tools._shared import _file_handle_key
    from app.services.builtin_tools.id_prefix import ambiguous_text, id_prefix

    if not user_id or not isinstance(file_id, str) or not file_id.strip():
        return None, None
    key = _file_handle_key(file_id).removeprefix("file-").strip()
    try:
        full = safe_uuid(key)
        if full is not None:
            return (full, None) if full in await _own_row_ids([full], user_id) else (None, None)
        prefix = id_prefix(key)
        if prefix is None:
            return None, None
        candidates = await _own_rows_by_prefix(prefix, user_id)
    except Exception as exc:
        logger.warning("Owned file lookup failed for %s: %s", key, format_exception_message(exc))
        return None, None
    if len(candidates) == 1:
        return candidates[0][0], None
    if not candidates:
        return None, None
    return None, ambiguous_text(file_id, "files of yours", candidates)


async def load_owned_file_ref(file_id: object, user_id: object) -> tuple[FileRef | None, str | None]:
    """The ``FileRef`` of the stored file :func:`resolve_owned_file_id` finds, or its refusal."""
    from app.llm.base import FileRef

    row_id, refusal = await resolve_owned_file_id(file_id, user_id)
    if row_id is None:
        return None, refusal
    try:
        row = (await _stored_rows([row_id])).get(row_id)
    except Exception as exc:
        logger.warning("Owned file %s not loaded: %s", row_id, format_exception_message(exc))
        return None, None
    return (FileRef.from_attachment(row), None) if row is not None else (None, None)


async def resolve_attachable_files(
    file_ids: Sequence[object],
    *,
    file_context: FileContext | None,
    conversation_id: object | None,
    assignment_run_id: object | None,
    user_id: object | None = None,
) -> AttachableFiles:
    """The files of *file_ids* the creating turn may attach to a work item.

    An id is admitted when the file is stored AND either the turn holds it -
    its ``FileContext`` (whatever spelling ``read_file_content`` accepts: the
    bare id, the ``[[FILE:id]]`` marker, an unambiguous filename) or a row
    bound to its *conversation_id* / *assignment_run_id* - or the acting
    *user_id* owns its row (:func:`_own_row_ids`), so a file of another
    conversation or run of the same user is attached by its id or marker. A
    file the turn only holds in memory, a template asset, a row of another
    user and a malformed id are refused. Duplicates collapse.
    """
    from app.llm.base import FileRef
    from app.services.builtin_tools._shared import (
        _file_handle_key,
        load_scoped_file_ref,
        resolve_file_ref,
    )

    wanted = [str(raw).strip() for raw in file_ids if str(raw or "").strip()]
    found: list[tuple[str, uuid.UUID | None]] = []
    for raw in wanted:
        ref = resolve_file_ref(file_context, raw)
        if ref is None:
            ref = await load_scoped_file_ref(
                raw,
                conversation_id=str(conversation_id) if conversation_id else None,
                assignment_run_id=str(assignment_run_id) if assignment_run_id else None,
            )
        found.append((raw, _row_uuid(ref.file_id) if ref is not None else None))
    # What the turn does not hold may still be a file its user owns: an event
    # run naming a file another conversation of the same user produced, by its
    # id or by an 8+ hexadecimal prefix of it.
    unheld = {raw: _row_uuid(_file_handle_key(raw)) for raw, row_id in found if row_id is None}
    ambiguous: dict[str, str] = {}
    if user_id:
        from app.services.builtin_tools.id_prefix import id_prefix

        for raw, row_id in list(unheld.items()):
            if row_id is not None or id_prefix(_file_handle_key(raw).removeprefix("file-")) is None:
                continue
            unheld[raw], refusal = await resolve_owned_file_id(raw, user_id)
            if refusal:
                ambiguous[raw] = refusal
    candidates = [row_id for row_id in unheld.values() if row_id is not None]
    if candidates and user_id:
        try:
            owned = await _own_row_ids(candidates, user_id)
        except Exception as exc:
            logger.warning("Work item attachment ownership lookup failed: %s", format_exception_message(exc))
            owned = set()
        found = [
            (raw, row_id if row_id is not None else (unheld[raw] if unheld[raw] in owned else None))
            for raw, row_id in found
        ]
    try:
        stored = await _stored_rows(row_id for _, row_id in found if row_id is not None)
    except Exception as exc:
        logger.warning("Work item attachment lookup failed: %s", format_exception_message(exc))
        stored = {}
    result = AttachableFiles(ambiguous=ambiguous)
    seen: set[uuid.UUID] = set()
    for raw, row_id in found:
        row = stored.get(row_id) if row_id is not None else None
        if row is None:
            result.refused.append(raw)
            continue
        if row_id in seen:
            continue
        seen.add(row_id)
        result.refs.append(FileRef.from_attachment(row))
    return result


def cited_file_ids(text: str | None) -> list[str]:
    """The file handles a work item result cites, in order, de-duplicated.

    Only the spellings a tool hands out are read - the ``[[FILE:<id>]]``
    marker and the ``file:<uuid>`` URL (``_shared.EMBED_FILE_REF_RE``) - never
    a bare uuid, which in a result is as often a work item, run or PR id. The
    caller admits them through :func:`resolve_attachable_files`, so a cited id
    the turn does not hold never becomes a grant.
    """
    from app.services.builtin_tools._shared import EMBED_FILE_REF_RE

    found = (m.group("marker") or m.group("uuid") for m in EMBED_FILE_REF_RE.finditer(text or ""))
    return list(dict.fromkeys(value.lower() for value in found if value))


def attachment_ids(item: dict | None) -> list[str]:
    """The attachment ids a serialized work item names, well-formed and de-duplicated."""
    raw = (item or {}).get("attachment_file_ids") or []
    if not isinstance(raw, list):
        return []
    ids = (_row_uuid(value) for value in raw)
    return list(dict.fromkeys(str(row_id) for row_id in ids if row_id is not None))


async def work_item_file_refs(item: dict | None) -> tuple[list[FileRef], int]:
    """The refs of the files a visible *item* names that are still stored.

    Returns ``(refs, gone)``: the refs in the order the creator attached them,
    and how many attached files no longer exist. The caller has already read
    *item* through the work-item visibility, which is the grant. Never raises:
    a lookup that fails costs the files (logged), not the read of the item.
    """
    from app.llm.base import FileRef

    ids = attachment_ids(item)
    if not ids:
        return [], 0
    try:
        stored = await _stored_rows(uuid.UUID(i) for i in ids)
    except Exception as exc:
        logger.warning(
            "Could not load the files of work item %s: %s",
            (item or {}).get("id"),
            format_exception_message(exc),
        )
        return [], 0
    refs = [FileRef.from_attachment(stored[uuid.UUID(i)]) for i in ids if uuid.UUID(i) in stored]
    return refs, len(ids) - len(refs)


async def resolve_readable_attachment(
    file_id: object,
    *,
    user_id: str | None,
    agent_id: str | None,
    agent_company_managed: bool = False,
) -> tuple[FileRef | None, str | None]:
    """The file a work item the agent may read attaches, named by its id or an id prefix.

    A brief cites the files of a sibling item by id or by the first 8+
    hexadecimal characters of it; this finds the one such file without the
    reader opening that item first. The grant is the one
    :func:`work_item_file_refs` documents - whoever may read an item reads the
    files it is about - checked the way the ``get_work_item`` tool checks it:
    the work-item visibility, the creator's narrowing for the agent, then the
    agent's coordination scope (its fleet) unless its user is an administrator.

    Returns ``(ref, None)`` for one stored file, ``(None, None)`` when nothing
    the agent may read matches (the caller answers its usual not found) and
    ``(None, refusal)`` naming the candidates when a prefix starts several.
    Never raises: a failed lookup is logged and reads as not found.
    """
    from app.llm.base import FileRef
    from app.services.builtin_tools._shared import _file_handle_key
    from app.services.builtin_tools.id_prefix import MAX_CANDIDATES, ambiguous_text, id_prefix

    if not agent_id or not user_id or not isinstance(file_id, str):
        return None, None
    key = _file_handle_key(file_id).removeprefix("file-").strip().lower()
    full = safe_uuid(key)
    prefix = str(full) if full is not None else id_prefix(key)
    if prefix is None:
        return None, None
    try:
        from app.services import coordination_scope
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import work_items_attaching

        items = await work_items_attaching(
            prefix,
            user_id=user_id,
            reader=agent_writer(is_company_managed=agent_company_managed, agent_id=agent_id),
        )
        if items:
            scope = await coordination_scope.resolve_coordination_scope(agent_id, user_id, "")
            outside = not isinstance(scope, str) and not all(scope.admits(i) for i in items)
            if outside and not await coordination_scope.user_is_admin(user_id):
                items = [i for i in items if scope.admits(i)]
        ids = list(dict.fromkeys(i for item in items for i in attachment_ids(item) if i.startswith(prefix)))
        stored = await _stored_rows(uuid.UUID(i) for i in ids) if ids else {}
    except Exception as exc:
        logger.warning("Work item attachment lookup failed for %s: %s", key, format_exception_message(exc))
        return None, None
    rows = [stored[uuid.UUID(i)] for i in ids if uuid.UUID(i) in stored]
    if len(rows) == 1:
        return FileRef.from_attachment(rows[0]), None
    if not rows:
        return None, None
    candidates = [(row.id, getattr(row, "filename", "") or "") for row in rows[: MAX_CANDIDATES + 1]]
    return None, ambiguous_text(file_id, "files attached to work items", candidates)


def register_work_item_files(file_context: FileContext | None, refs: Sequence[FileRef]) -> bool:
    """Put *refs* in the reader's *file_context* as files it fetched to read.

    ``False`` when there is no context to register them in (a gate check, a
    direct tool call): the files can be named, not opened.
    """
    from app.tools.base import register_fetched_file

    if file_context is None:
        return False
    for ref in refs:
        register_fetched_file(file_context, ref)
    return True


__all__ = [
    "MAX_DISPATCH_INLINED_ATTACHMENTS",
    "MAX_WORK_ITEM_ATTACHMENTS",
    "MAX_WORK_ITEM_ATTACHMENTS_PER_CALL",
    "AttachableFiles",
    "attachment_cap_refusal",
    "attachment_ids",
    "cited_file_ids",
    "load_owned_file_ref",
    "register_work_item_files",
    "resolve_attachable_files",
    "resolve_owned_file_id",
    "resolve_readable_attachment",
    "work_item_file_refs",
]
