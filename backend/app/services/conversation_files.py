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

"""The files of a conversation, from its file store, for every turn.

A chat turn used to build its :class:`~app.tools.base.FileContext` from the
current message's files and the ``files`` of the history messages that
survived compaction. A file a tool stored in an earlier turn (an integration
download, a generated file) hangs off a message that compaction may drop, and
a resumed turn after a rollout rebuilds its history the same way: the file's
``FileAttachment`` row and its bytes still existed, but every tool resolving
it by id (an upload, an attachment, ``read_file_content``) answered "not
found in this conversation".

:func:`conversation_file_refs` reads the conversation's stored files on a
short own session (the turn released its pooled connection before the loop)
and :func:`register_conversation_files` adds them to the turn's
context. A file already bound to a message counts as one the conversation
already knew: never attached to this turn's reply. A file no message shows
yet (``message_id`` NULL: written by a turn that failed or was cut before
its reply was stored) is resolvable but stays new to the turn, so a later
turn that reuses it (``_find_existing_tool_file`` answers that row for the
same bytes) still attaches it to its reply. The history files are added
after them, so a ref a message carries (inline bytes, a fresher name) wins.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.utils import format_exception_message, safe_uuid

if TYPE_CHECKING:
    from app.llm.base import FileRef

logger = logging.getLogger(__name__)

#: Stored files one turn loads at most, the most recent ones. A conversation
#: is not a file store; past this bound the oldest are left out.
MAX_CONVERSATION_FILES = 500


@dataclass(frozen=True)
class StoredConversationFile:
    """One stored file of the conversation and whether a message shows it."""

    ref: FileRef
    #: True when the row is bound to a message (``message_id`` set).
    bound: bool


async def conversation_file_refs(
    conversation_id: object, *, limit: int = MAX_CONVERSATION_FILES
) -> list[StoredConversationFile]:
    """The conversation's stored files, oldest first (at most *limit*, the newest).

    Template assets are left out. Best effort: a failed read answers ``[]``
    and the turn keeps the files its messages carry.
    """
    from app.core.database import async_session
    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    conv_uuid = conversation_id if isinstance(conversation_id, uuid.UUID) else safe_uuid(str(conversation_id or ""))
    if conv_uuid is None:
        return []
    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(FileAttachment)
                    .where(
                        FileAttachment.conversation_id == conv_uuid,
                        FileAttachment.template_container_id.is_(None),
                    )
                    .order_by(FileAttachment.created_at.desc())
                    .limit(max(1, limit))
                )
            ).scalars().all()
            refs = [
                StoredConversationFile(
                    ref=FileRef.from_attachment(row),
                    bound=getattr(row, "message_id", None) is not None,
                )
                for row in rows
            ]
    except Exception as exc:
        logger.warning(
            "Could not load the stored files of conversation %s: %s",
            conv_uuid,
            format_exception_message(exc),
        )
        return []
    refs.reverse()
    return refs


def register_conversation_files(
    file_context: object | None,
    stored: Iterable[StoredConversationFile],
    known_ids: set[str] | None = None,
) -> int:
    """Add the *stored* files the context does not hold yet.

    *known_ids* is the turn's set of files it started with (the ones never
    attached to its reply): only a file bound to a message goes there; an
    unbound one stays new to the turn. An unbound one enters as a fetched
    file (``add_fetched``): a file an earlier turn only fetched and
    never named stays unbound for good, so a plain add would attach every
    such file to every later reply. The reply carries an unbound file when
    its text names it, or when a tool of the turn hands it back (the dedup
    of ``_find_existing_tool_file``, whose write clears the fetched mark).
    Answers how many refs were added.
    """
    if file_context is None:
        return 0
    get = getattr(file_context, "get", None)
    add = getattr(file_context, "add", None)
    if not callable(add):
        return 0
    add_fetched = getattr(file_context, "add_fetched", None)
    add_unbound = add_fetched if callable(add_fetched) else add
    added = 0
    for item in stored:
        ref = item.ref
        file_id = str(getattr(ref, "file_id", "") or "")
        if not file_id:
            continue
        if known_ids is not None and item.bound:
            known_ids.add(file_id)
        if callable(get) and get(file_id) is not None:
            continue
        (add if item.bound else add_unbound)(ref)
        added += 1
    return added
