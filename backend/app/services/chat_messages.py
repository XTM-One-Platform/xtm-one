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

"""Chat messages read the same way by every surface that serves them.

The web chat (``api/v1/chat.py``), the platform chat (``api/v1/platform.py``),
the channel router's history and the background-task wake all page a
conversation's messages, decode their ``metadata_json`` and render their file
attachments. The rules live here, once:

- pagination: :func:`message_order_desc`, :func:`message_order_asc`,
  :func:`message_cursor_filter`, :func:`slice_message_page`, and
  :func:`read_visible_message_page` for a page a reader sees, hidden platform
  wakes (:func:`message_is_hidden`, in SQL :func:`message_shown_clause`) left out;
- metadata: :func:`parse_message_metadata`, :func:`parse_attachments`,
  :func:`attachment_download_url`;
- the attachment block stored for an agent-generated file:
  :func:`attachment_meta_from_file_attachment`;
- the text of a web chat send or steer: :func:`sent_message_text`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, or_

from app.models.chat import Message


def message_order_desc() -> tuple:
    """Newest-first ordering used by message pagination queries.

    ``sequence`` is the canonical per-conversation ordering; ``created_at``
    and ``id`` are stable tiebreakers for rows that share a sequence value
    (e.g. legacy rows inserted with the default ``sequence=0``).
    """
    return (
        Message.sequence.desc(),
        Message.created_at.desc(),
        Message.id.desc(),
    )


def message_order_asc() -> tuple:
    """Oldest-first twin of :func:`message_order_desc`: a conversation read
    from its start (``read_conversation_history``)."""
    return (
        Message.sequence.asc(),
        Message.created_at.asc(),
        Message.id.asc(),
    )


def message_cursor_filter(anchor: Message):
    """SQL filter matching messages strictly OLDER than *anchor*.

    Mirrors ``message_order_desc()``: a row is older when its
    ``(sequence, created_at, id)`` triple sorts before the anchor's.
    """
    return or_(
        Message.sequence < anchor.sequence,
        and_(
            Message.sequence == anchor.sequence,
            Message.created_at < anchor.created_at,
        ),
        and_(
            Message.sequence == anchor.sequence,
            Message.created_at == anchor.created_at,
            Message.id < anchor.id,
        ),
    )


def slice_message_page(rows: list, limit: int) -> tuple[list, bool]:
    """Turn a newest-first ``limit + 1`` fetch into a chronological page.

    *rows* must have been fetched with ``message_order_desc()`` and
    ``LIMIT limit + 1`` — the sentinel extra row only signals that more
    history exists and is never returned.
    """
    has_more = len(rows) > limit
    page = list(rows[:limit])
    page.reverse()
    return page, has_more


async def read_visible_message_page(db, criteria: list, limit: int) -> tuple[list, bool]:
    """The *limit* newest messages matching *criteria* a reader sees, chronological, and whether older ones exist.

    One query: a hidden platform wake is left out in SQL
    (:func:`message_shown_clause`), so a run of them neither empties a page
    whose older history the reader could still page into, nor reports older
    history made only of hidden rows.
    """
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(Message)
            .where(*criteria, message_shown_clause())
            .order_by(*message_order_desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    return slice_message_page(list(rows), limit)


def message_shown_clause():
    """SQL of the messages a reader sees: :func:`message_is_hidden` in the query.

    The same rule: the metadata a wake is stored with, compared whole, so a
    message whose metadata only mentions the wake stays shown.
    """
    from app.services.parent_wake import PARENT_WAKE_METADATA

    return Message.metadata_json.is_distinct_from(PARENT_WAKE_METADATA)


def message_is_hidden(message: Message) -> bool:
    """True for a platform-injected wake message: stored, never shown to a reader.

    A wake is the background-task wake (``parent_wake``), the one writer of a
    hidden message, told by the metadata it is stored with and never rewritten.
    """
    from app.services.parent_wake import PARENT_WAKE_METADATA

    return message.metadata_json == PARENT_WAKE_METADATA


def parse_message_metadata(metadata_json: str | None) -> dict:
    """Parse message metadata_json into a dict, returning {} on failure.

    Guards against both JSON-decode failures AND well-formed JSON that
    parses to something other than a dict (``json.loads("[]")`` →
    ``[]``).  Every caller assumes ``.get(...)`` works on the return value,
    so a non-dict result would otherwise propagate an ``AttributeError`` up
    through the chat list / message endpoints — unlike
    ``agent_chats.py::_parse_msg_attachments`` which wraps the whole decode
    in a ``try/except`` and silently returns ``None``, this is called from
    many places without such a rescue, so the guarantee belongs here at the
    source.
    """
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def attachment_download_url(
    file_id: str,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> str:
    if conversation_id and message_id:
        return (
            f"/api/v1/chat/conversations/{conversation_id}/messages/"
            f"{message_id}/files/{file_id}/download"
        )
    return f"/api/v1/chat/files/{file_id}/download"


def parse_attachments(
    metadata_json: str | None,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> list[dict] | None:
    """Extract attachment metadata from a message's metadata_json field.

    If attachments contain a ``file_id``, a download URL is generated
    pointing to the backend file download endpoint.

    Defensive against malformed metadata at every layer:
    ``parse_message_metadata`` already guarantees a ``dict`` return, but
    ``meta["attachments"]`` can still be any JSON value (string, int, dict,
    ``None``), and individual entries inside a list can be non-dicts.  Both
    are silently skipped here so a single legacy or hand-crafted bad row
    cannot crash the enclosing message-serialisation path.  Matches the
    ``channels/router.py`` and ``agent_chats.py`` decode contracts.
    """
    meta = parse_message_metadata(metadata_json)
    attachments = meta.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return None
    valid: list[dict] = [att for att in attachments if isinstance(att, dict)]
    if not valid:
        return None
    for att in valid:
        file_id = att.get("file_id")
        if file_id:
            att["download_url"] = attachment_download_url(
                file_id,
                conversation_id=conversation_id,
                message_id=message_id,
            )
    return valid


def attachment_meta_from_file_attachment(file_att: object) -> dict[str, object]:
    """Build the JSON-serialisable attachment block stored in
    ``Message.metadata_json`` for an agent-generated file.

    The shape returned here is what the frontend ``FileChip`` /
    ``WorkingFileChip`` components consume; see
    ``resolveAttachmentMime`` + ``mimeVisual`` in
    ``frontend/src/pages/chat/ChatMessages.tsx``.

    ``type`` is the short "extension label" the chip surfaces under
    the filename ("PDF", "JSON", "YAR", …). It used to default to
    ``"txt"`` whenever the filename had no extension — which mislabels
    every PDF / JSON / CSV produced via ``generate_file`` that the
    agent forgets to suffix. We now derive a sensible fallback from
    ``content_type`` (``application/pdf`` → ``"pdf"``, ``text/csv`` →
    ``"csv"``, ``application/yaml`` → ``"yaml"``, …) so the chip never
    silently mislabels a file just because the agent skipped the
    extension. The legacy ``"txt"`` last-ditch fallback only fires for
    files with neither an extension nor an inferable content type.
    """
    filename = str(getattr(file_att, "filename", "file"))
    raw_content_type = str(
        getattr(file_att, "content_type", "") or "application/octet-stream"
    )
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
    else:
        # Derive from the content-type subtype as the next-best label.
        # ``application/pdf`` → ``pdf``; ``image/svg+xml`` → ``svg``;
        # ``text/markdown`` → ``markdown``. For vendor subtypes we take
        # the last dotted segment, so
        # ``application/vnd.openxmlformats-...spreadsheetml.sheet`` →
        # ``sheet`` rather than the unwieldy full subtype.
        slash = raw_content_type.find("/")
        subtype = raw_content_type[slash + 1 :] if slash != -1 else ""
        # Drop ``+suffix`` (``+xml`` / ``+json``) and any ``vnd.`` prefix.
        subtype = subtype.split("+", 1)[0].split(";", 1)[0].strip().lower()
        if subtype.startswith("vnd."):
            subtype = subtype.rsplit(".", 1)[-1]
        # Final fallback so the chip never renders an empty label.
        ext = subtype or "txt"

    file_id = str(file_att.id)  # type: ignore[attr-defined]
    # ``source`` is a provenance category in the DB, not a display tag:
    # the ``generate_file`` tool writes ``agent_working`` / ``agent_download``
    # (see ``build_generate_file_tool`` in ``builtin_tools/media.py``).  Map
    # the working-file provenances (``agent_working``, and ``agent_check``
    # for a check's picture) to the ``working_file`` display tag
    # and default everything else (``agent_download``, ``chat``,
    # ``assignment``, …) to ``download_file`` so the frontend renders
    # the full download card.
    from app.llm.base import is_working_file_source

    file_tag = "working_file" if is_working_file_source(getattr(file_att, "source", None)) else "download_file"
    meta: dict[str, object] = {
        "filename": filename,
        "type": ext,
        "size": int(getattr(file_att, "size", 0) or 0),
        "file_id": file_id,
        "content_type": raw_content_type,
        "file_tag": file_tag,
    }
    # What tells two same-name files apart on their cards: when the file was
    # written and the digest of its bytes (NULL for an upload).
    created_at = getattr(file_att, "created_at", None)
    if isinstance(created_at, datetime):
        meta["created_at"] = created_at.isoformat()
    sha256 = getattr(file_att, "sha256", None)
    if isinstance(sha256, str) and sha256:
        meta["sha256"] = sha256
    return meta


def sent_message_text(content: str | None, file_ids: Iterable[str] | None) -> str:
    """The text a web chat message carries: what was typed, or, for files sent alone, a request to read them.

    The composer sends attachments with no text at all; the agent is then
    asked to analyze them, as the chat page always did. Empty with no file,
    the message the send and the steer refuse.
    """
    text = (content or "").strip()
    if text:
        return text
    count = sum(1 for file_id in file_ids or () if file_id)
    if not count:
        return ""
    return f"Please analyze the attached {count} file{'' if count == 1 else 's'}."
