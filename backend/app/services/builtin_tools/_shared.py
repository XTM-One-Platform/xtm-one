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

"""Shared private helpers used by multiple ``builtin_tools`` submodules.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import FileRef
    from app.tools.base import FileContext, ToolResult

# Single logger shared by every submodule so log records keep the
# historical ``app.services.builtin_tools`` logger name (the test-suite
# and log filtering rely on it).
logger = logging.getLogger(__package__)

_TASK_ID_EXAMPLE = "550e8400-e29b-41d4-a716-446655440000"


def normalize_tool_error(result: dict) -> dict:
    """Normalize a helper's string-valued ``error`` into the canonical shape.

    Built-in tool helpers may return ``{"error": "<reason>", ...}`` for
    readability, but callers (and downstream automation) should be able to
    branch on a boolean ``error`` flag. When ``result["error"]`` is a string,
    rewrite it to ``{"error": True, "message": "<reason>", ...}`` while keeping
    every other field. A dict that already uses a boolean ``error`` (or has no
    ``error`` key) is returned unchanged. Shared so the FIMI tool wrappers emit
    one consistent error contract.
    """
    err = result.get("error")
    if isinstance(err, str):
        normalized = {k: v for k, v in result.items() if k != "error"}
        normalized["error"] = True
        normalized["message"] = err
        return normalized
    return result


def _is_uuid_like(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        # stix2 requires RFC 4122 variant (bits 6-7 of byte 8 must be 0b10xx).
        # Python's uuid.UUID parses any hex string, so we must check the variant
        # explicitly — e.g. "c3d4e5f6-a7b8-4c9d-0e1f-..." has NCS variant and
        # is rejected by stix2 even though it parses fine in Python.
        return parsed.variant == uuid.RFC_4122
    except (ValueError, AttributeError, TypeError):
        return False


_TASK_ID_DISPLAY_BUDGET = 119  # max chars of caller-controlled value to echo


def _bounded_display(value: object, budget: int = _TASK_ID_DISPLAY_BUDGET) -> str:
    """Bound the displayed ``repr`` of a caller-controlled value for an error.

    Tool arguments come from JSON/LLM output, so a hallucinated value can carry
    a multi-MB string / bytes blob or a pathological container. The work is
    bounded *before* any potentially expensive ``repr()`` so the formatter
    never allocates the same volume of memory just to truncate it back down to
    ~``budget`` chars:

    * ``str`` / ``bytes`` / ``bytearray`` — slice the *source* to ``budget``
      chars first, then ``repr`` the slice (a trailing ``…`` marks that the
      value was longer; inputs under the budget round-trip verbatim). O(budget)
      regardless of input size.
    * Atomic scalars (``bool`` / ``int`` / ``float`` / ``None``) — ``repr`` is
      already cheap, so use it directly.
    * Everything else (dict / list / tuple / set / arbitrary objects) — a fixed
      ``<X>`` type-name placeholder (e.g. ``<dict>``, ``<list>``), avoiding a
      walk of a 10K-element container or an expensive ``__repr__`` on an
      unknown object.
    """
    if isinstance(value, (str, bytes, bytearray)):
        if len(value) > budget:
            return f"{value[:budget]!r}…"
        return repr(value)
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return f"<{type(value).__name__}>"


def _invalid_task_id_message(task_id: object) -> str:
    """Compose a user-facing error pointing at a malformed ``task_id``.

    The display value is bounded via :func:`_bounded_display` so a hallucinated
    ``task_id`` carrying a multi-MB string / bytes blob / pathological container
    can't force the formatter to allocate the same volume of memory just to
    truncate the result back down.
    """
    return (
        "Invalid task_id format: expected a UUID string "
        f"(e.g., '{_TASK_ID_EXAMPLE}'), got: {_bounded_display(task_id)}"
    )


def _own_session():
    """Return a fresh short-lived DB session for artifact persistence.

    Kept as a module-level seam so unit tests can monkeypatch it with a
    fake session factory (no real database needed).
    """
    from app.core.database import async_session

    return async_session()


#: ``FileAttachment.source`` prefix that OPTS a write into the conversation-
#: scoped dedup — the agent-provenance family (``agent``, ``agent_generated``,
#: ``agent_download``, ``agent_working``). A user upload (``chat``,
#: ``public_chat``, ``assignment``), a runner capture (evidence: one row per
#: capture, whatever the pixels) or a template asset never looks for a
#: duplicate. Within the family the match is on the EXACT source: ``source``
#: is what decides how the file is shown (``agent_working`` is the compact
#: working-file chip, every other value a download card), so handing an
#: ``agent_download`` row to an ``agent_working`` write would change what the
#: user sees, not just where the bytes live.
TOOL_FILE_DEDUP_SOURCE_PREFIX = "agent"


def file_sha256(raw_bytes: bytes) -> str:
    """Hex SHA-256 of a file's bytes — the identity the dedup matches on."""
    return hashlib.sha256(raw_bytes).hexdigest()


async def _file_sha256_off_loop(raw_bytes: bytes) -> str:
    """:func:`file_sha256` computed in a worker thread.

    A tool file can be as large as the 40 MB upload cap and this runs mid
    agentic loop, next to other turns' LLM streams; hashlib releases the GIL
    on large buffers, so the thread is free and a big file never stalls the
    event loop (``llm.file_resolver._content_digest`` hashes attachments the
    same way).
    """
    return await asyncio.to_thread(file_sha256, raw_bytes)


async def _find_existing_tool_file(
    *, conversation_id: uuid.UUID, sha256: str, source: str, filename: str | None = None
) -> object | None:
    """The row of the same *source* already holding these bytes in this conversation.

    Oldest first, so every later duplicate converges on ONE row. Only a row
    that is nobody's run output (``assignment_run_id IS NULL``) qualifies: the
    assignment engine re-points every file in its run's ``FileContext`` to
    that run, so reusing another run's output row would re-home it. Today no
    conversation-scoped row carries a run id (assignment runs persist with a
    run scope and no conversation, so they never look up) — the predicate
    makes that structural rather than incidental. Only a row no message shows
    yet (``message_id IS NULL``) qualifies either: a row is shown by at most
    one message (``sub_agent_files.attachable_filter``), so a later answer
    handed an earlier answer's row could not carry it, and the embedded chat
    and the widget, which show a reply's files from that reply alone, showed
    nothing. *filename*, when given, must match too: a write that names its
    file is a file of that name. Fails open: a lookup error
    is logged and ``None`` is returned — dedup is a saving, never a reason not
    to persist. Runs on its own short-lived session, like the write, so no
    connection is pinned across the S3 upload.
    """
    from sqlalchemy import select

    from app.models.file_attachment import FileAttachment

    conditions = [
        FileAttachment.conversation_id == conversation_id,
        FileAttachment.sha256 == sha256,
        FileAttachment.source == source,
        FileAttachment.assignment_run_id.is_(None),
        FileAttachment.message_id.is_(None),
    ]
    if filename is not None:
        conditions.append(FileAttachment.filename == filename)
    try:
        async with _own_session() as own_db:
            result = await own_db.execute(
                select(FileAttachment)
                .where(*conditions)
                .order_by(FileAttachment.created_at)
                .limit(1)
            )
            return result.scalars().first()
    except Exception as exc:
        logger.warning(
            "Tool-file dedup lookup failed (persisting a new file instead): %s",
            format_exception_message(exc),
        )
        return None


async def _persist_generated_file(
    *,
    db: AsyncSession | None = None,
    user_id: str,
    filename: str,
    raw_bytes: bytes,
    content_type: str | None,
    conversation_id: str | None,
    assignment_run_id: str | None,
    source: str = "agent_generated",
    image_meta: dict | None = None,
    match_filename: bool = False,
    description: str | None = None,
) -> object:
    """Persist an agent-generated file artifact to S3 + the DB.

    The S3 prefix is built from the *canonical* string form of the
    parsed UUID (``str(conv_uuid)``) rather than the raw caller-provided
    string — this normalises casing and any leading ``urn:uuid:``
    prefix so two callers passing semantically-equal identifiers end
    up with the same object-name prefix and the bucket listing stays
    grep-able. ``safe_uuid`` does NOT strip surrounding whitespace
    (``uuid.UUID(" 550e…")`` raises), so a caller passing whitespace
    around the id falls through to the ``"generated"`` prefix.

    .. important:: **Never writes on the caller's session.**  Tool
       closures invoke this helper *mid agentic loop*, after the caller
       deliberately released its pooled connection via
       ``release_db_connection_before_loop``.  A ``flush()`` on that
       session would re-check-out a pooled connection and pin it across
       every remaining LLM call of the loop (minutes) — one of the main
       drivers of the production "Pool near exhaustion" warnings.
       The row is instead written and committed on a
       dedicated short-lived session, holding a connection for
       milliseconds.  The ``db`` parameter is retained for signature
       compatibility and is intentionally unused.

    .. note:: **Byte-identical tool files are one file per conversation.**
       Before writing, an agent-provenance ``source`` (see
       :data:`TOOL_FILE_DEDUP_SOURCE_PREFIX`) looks for a row of the SAME
       source with the same SHA-256 in the SAME conversation, not bound to
       any assignment run nor shown by any message yet, and returns it
       instead of creating another row and another S3 object — the file the
       caller gets is exactly the bytes it asked to store, under the id the
       conversation already knows (so its ``filename`` is the stored row's,
       not the name this call asked for, unless *match_filename* makes the
       name part of the match: a file the agent writes for the user under a
       name is that name), shown the way this source is shown. Trace 722375f1 (2026-09-24)
       persisted 84 byte-identical PNGs from one turn; they are now 3. User
       uploads never set ``sha256`` and are never matched. The lookup and the
       write are two short sessions with the S3 upload in between, so two
       genuinely concurrent first writes of the same bytes can still both
       land — the intra-batch CORE-6 dedup is what prevents that for
       identical calls. The digest is recorded for every file persisted
       THROUGH this helper (downloads, ``generate_file``, the code
       interpreter, MCP binary content, presentation exports, ...); the one
       tool path that still writes its own row is outside it by design:
       ``generate_image`` (``media.py`` — non-deterministic bytes next to its
       legacy ``ChatImage`` row).

    *image_meta* is the image geometry a logo wall draws the mark by
    (``app.services.image_geometry``), stored with the row - and given to a
    reused byte-identical row that was stored without it. *description* is
    what the file shows, for a reader who cannot see it (the column
    ``annotate_file`` writes, read back as the file's notes): stored the same
    way, a reused row's own description kept.
    """
    del db  # persistence uses an independent short-lived session

    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage, guess_content_type

    conv_uuid = safe_uuid(conversation_id)
    run_uuid = safe_uuid(assignment_run_id)
    user_uuid = safe_uuid(user_id)
    digest = await _file_sha256_off_loop(raw_bytes)
    if conv_uuid is not None and (source or "").startswith(TOOL_FILE_DEDUP_SOURCE_PREFIX):
        existing = await _find_existing_tool_file(
            conversation_id=conv_uuid,
            sha256=digest,
            source=source,
            filename=filename if match_filename else None,
        )
        if existing is not None:
            logger.info(
                "Reusing file %s for byte-identical tool file '%s' in conversation %s "
                "(sha256 %s)",
                getattr(existing, "id", "?"),
                filename,
                conv_uuid,
                digest[:12],
            )
            if image_meta is not None and getattr(existing, "image_meta", None) is None:
                await _store_image_meta(existing.id, image_meta)
            if description and not getattr(existing, "description", None):
                await store_description(existing, description)
            return existing
    effective_content_type = content_type or guess_content_type(filename)
    # Normalize to a lowercase base MIME type (drop ``; charset=...`` params and
    # casing) at the single persistence point, so EVERY caller —
    # ``build_generate_file_tool``, custom-tool ``$output_files``, and any
    # future caller — stores a consistent value. Downstream code does
    # case/parameter-sensitive checks (``startswith("image/")`` for
    # inline-vs-download, chip label/icon, and the ``file_tag`` mapping), which
    # mixed-case or parameterized inputs (``Image/PNG``, ``text/plain;
    # charset=utf-8``) would silently break. ``generate_file`` already applies
    # the same normalization before calling in (it needs the base type for its
    # PDF-render branch), so re-applying here is an idempotent no-op for it.
    if effective_content_type:
        effective_content_type = effective_content_type.split(";", 1)[0].strip().lower()

    if conv_uuid:
        s3_prefix = f"chat/{conv_uuid}"
    elif run_uuid:
        s3_prefix = f"assignments/{run_uuid}"
    else:
        s3_prefix = "generated"

    s3_object_name = await file_storage.upload(
        data=raw_bytes,
        filename=filename,
        content_type=effective_content_type,
        prefix=s3_prefix,
    )

    file_att = FileAttachment(
        id=uuid.uuid4(),
        user_id=user_uuid,
        conversation_id=conv_uuid,
        assignment_run_id=run_uuid,
        filename=filename,
        content_type=effective_content_type,
        size=len(raw_bytes),
        s3_object_name=s3_object_name,
        source=source,
        sha256=digest,
        image_meta=image_meta,
        description=description,
    )
    async with _own_session() as own_db:
        own_db.add(file_att)
        await own_db.commit()
    return file_att


async def store_description(row: Any, description: str) -> None:
    """Give a file stored without one the *description* of what it shows; never a template asset's caption.

    Best effort, on its own short session: a failure leaves the file as it was.
    The row in hand is updated too, so a ``FileRef`` built from it carries the notes.
    """
    from sqlalchemy import update

    from app.models.file_attachment import FileAttachment

    try:
        async with _own_session() as own_db:
            await own_db.execute(
                update(FileAttachment)
                .where(
                    FileAttachment.id == row.id,
                    FileAttachment.description.is_(None),
                    FileAttachment.template_container_id.is_(None),
                )
                .values(description=description)
            )
            await own_db.commit()
        row.description = description
    except Exception as exc:
        logger.debug("Description not stored on file %s: %s", row.id, format_exception_message(exc))


async def _store_image_meta(file_id: object, image_meta: dict) -> None:
    """Give a reused row the image geometry it was stored without.

    Best effort, on its own short session like every write here: a failure
    leaves the row to the deck save's backfill, never fails the tool.
    """
    from sqlalchemy import update

    from app.models.file_attachment import FileAttachment

    try:
        async with _own_session() as own_db:
            await own_db.execute(
                update(FileAttachment)
                .where(FileAttachment.id == file_id, FileAttachment.image_meta.is_(None))
                .values(image_meta=image_meta)
            )
            await own_db.commit()
    except Exception as exc:
        logger.debug("Image geometry not stored on reused file %s: %s", file_id, format_exception_message(exc))


async def persist_tool_artifact(
    *,
    db: AsyncSession | None = None,
    user_id: str,
    filename: str,
    raw_bytes: bytes,
    content_type: str | None,
    conversation_id: str | None,
    assignment_run_id: str | None,
    file_context: object | None = None,
    source: str = "agent_generated",
    image_meta: dict | None = None,
    fetched: bool = False,
) -> object:
    """Persist a tool-produced binary artifact and register it on the loop.

    Thin wrapper over :func:`_persist_generated_file` that also builds a
    :class:`~app.llm.base.FileRef` and adds it to *file_context* (when
    provided) so the post-loop chat layer (``api/v1/chat.py``) and the
    assignment engine attach it to the conversation / assignment run exactly
    like ``generate_file`` and ``generate_image`` do — the single persistence
    path for *every* agent-produced file. Returns the ``FileRef`` (the caller
    decides how to surface it: an inline ``![](…)`` markdown image for
    screenshots / charts, or a ``[[FILE:id]]`` marker for downloadable files).
    *fetched* registers a file the tool brought in to READ (a fetched image,
    a knowledge-base document copy): a reply shows it only when it names it
    (``register_fetched_file``).

    Like :func:`_persist_generated_file`, this never writes on the caller's
    session — persistence happens on a dedicated short-lived session so a
    mid-loop tool call cannot pin the caller's pooled connection.
    """
    from app.llm.base import FileRef

    file_att = await _persist_generated_file(
        db=db,
        user_id=user_id,
        filename=filename,
        raw_bytes=raw_bytes,
        content_type=content_type,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        source=source,
        image_meta=image_meta,
    )
    file_ref = FileRef.from_attachment(file_att)
    from app.tools.base import register_fetched_file, register_produced_file

    if fetched:
        register_fetched_file(file_context, file_ref)
    else:
        register_produced_file(file_context, file_ref)
    return file_ref


def _platform_label() -> str:
    """Return the user-facing platform name from the branding cache."""
    from app.services.branding import get_cached_branding

    return get_cached_branding().name


# ---------------------------------------------------------------------------
# Conversation file handles
# ---------------------------------------------------------------------------
#
# How a model spells a file id, defined ONCE. ``generate_file`` hands back a
# ``[[FILE:<id>]]`` marker and ``generate_file``'s Markdown accepts a
# ``file:<uuid>`` URL, so both spellings come back in the next tool call's
# arguments — and a model that saw neither will type the filename. Every tool
# that takes a ``file_id`` resolves it through :func:`resolve_file_ref`, or the
# advertised marker would fail the call that copied it verbatim.
EMBED_FILE_REF_RE = re.compile(
    r"\[\[FILE:(?P<marker>[0-9a-fA-F-]{6,})\]\]"
    r"|file:(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass(frozen=True, slots=True)
class TurnExport:
    """A file an export tool (``export_presentation``, ``export_report``) stored.

    The tool builder keeps one per document, revision, format and resolved
    template for the turn (or run) it serves, so exporting a document nobody
    changed since hands back the same file instead of rendering it again.
    """

    file_ref: FileRef
    filename: str
    size: int
    #: The export log: what the file could not carry as the viewer shows it.
    notes: tuple[str, ...] = ()


def export_result(title: str, label: str, export: TurnExport, *, again: bool = False) -> ToolResult:
    """The export tools' answer: the file, its marker, and what the export could not carry.

    *again* says the document had not changed since *export* was stored, so
    the file is the one an earlier call of the same turn (or run) returned.
    """
    from app.tools.base import ToolResult

    described = f"`{export.filename}` ({export.size // 1024} KB)"
    head = (
        f"**{title}** has not changed since you exported it as {label}: {described}, the same file."
        if again
        else f"Exported **{title}** as {label}: {described}."
    )
    notes = "\nExport notes:\n" + "\n".join(f"- {note}" for note in export.notes) if export.notes else ""
    return ToolResult(
        tool_call_id="",
        content=(
            f"{head}\n[[FILE:{export.file_ref.file_id}]]\n"
            f"Include the [[FILE:...]] marker verbatim in your reply so the user gets the download.{notes}"
        ),
        files=[export.file_ref],
    )


def list_available_files(file_context: FileContext | None, limit: int = 20) -> str:
    """The "Available files" block appended to a file tool's error.

    Past *limit* files the MOST RECENT ones are listed: the file a refused call
    was after is usually the one the run just downloaded or wrote, and a
    listing of the oldest twenty hid exactly that one behind "... and N more".
    """
    from app.tools.base import quoted_file_notes

    if file_context is None or file_context.is_empty:
        return "No files are attached to this conversation."
    refs = file_context.list_files()
    lines = []
    if len(refs) > limit:
        lines.append(f"- ... {len(refs) - limit} earlier file(s) not listed")
    for r in refs[-limit:]:
        line = (
            f'- file_id="{r.file_id}" filename="{r.filename}" ({r.content_type}, '
            f"{format_file_size(r.size)})"
        )
        notes = quoted_file_notes(getattr(r, "notes", None))
        if notes:
            line += f" - notes: {notes}"
        lines.append(line)
    return "Available files:\n" + "\n".join(lines)


def _file_handle_key(file_id: str) -> str:
    """*file_id* as the model spelled it, reduced to the id or the filename it names."""
    key = file_id.strip()
    handle = EMBED_FILE_REF_RE.fullmatch(key)
    if handle:
        key = handle.group("marker") or handle.group("uuid")
    return key


def resolve_file_ref(file_context: FileContext | None, file_id: str | None):
    """Find a conversation file by id, or by exact filename when unambiguous.

    The id is accepted however the model spelled it: bare, ``file-<id>``, the
    ``[[FILE:<id>]]`` marker it copies straight out of a previous tool result,
    or the ``file:<uuid>`` URL form - the tool schemas advertise the marker,
    so it must resolve instead of failing the call.
    """
    if file_context is None or not file_id or not isinstance(file_id, str):
        return None
    key = _file_handle_key(file_id)
    ref = file_context.get(key)
    if ref is not None:
        return ref
    wanted = key.lower()
    by_name = [r for r in file_context.list_files() if r.filename.lower() == wanted]
    if len(by_name) == 1:
        return by_name[0]
    by_prefix = _context_files_by_prefix(file_context, key)
    if len(by_prefix) == 1:
        return by_prefix[0]
    return None


def _context_files_by_prefix(file_context: FileContext, key: str) -> list[Any]:
    """The files of *file_context* whose id starts with *key*, an 8+ hexadecimal id prefix
    (``id_prefix.id_prefix``); ``[]`` when *key* is no such prefix."""
    from app.services.builtin_tools.id_prefix import id_prefix

    prefix = id_prefix(key.removeprefix("file-"))
    if prefix is None:
        return []
    return [r for r in file_context.list_files() if str(r.file_id).lower().removeprefix("file-").startswith(prefix)]


def unresolved_file_message(file_context: FileContext | None, file_id: object) -> str:
    """The error a file tool answers when *file_id* names no single file of the context.

    A filename several files share is not a missing file: the answer names
    them by id so the next call can pick one, instead of claiming nothing is
    attached (a run that downloads a document it generated earlier holds two
    files of that name).
    """
    if file_context is not None and isinstance(file_id, str) and file_id.strip():
        wanted = _file_handle_key(file_id).lower()
        same_name = [r for r in file_context.list_files() if r.filename.lower() == wanted]
        if len(same_name) > 1:
            shown = same_name[-6:]
            ids = ", ".join(f'file_id="{r.file_id}" ({format_file_size(r.size)})' for r in shown)
            more = f" and {len(same_name) - len(shown)} earlier one(s)" if len(same_name) > len(shown) else ""
            return (
                f"Error: {len(same_name)} files of this conversation are named '{file_id.strip()}' - "
                f"pass the file_id of the one you mean (newest last): {ids}{more}."
            )
        same_start = _context_files_by_prefix(file_context, _file_handle_key(file_id))
        if len(same_start) > 1:
            from app.services.builtin_tools.id_prefix import MAX_CANDIDATES, ambiguous_text

            candidates = [(r.file_id, r.filename) for r in same_start[: MAX_CANDIDATES + 1]]
            return ambiguous_text(file_id, "files of this conversation", candidates)
    return (
        f"Error: no file '{file_id}' is attached to this conversation. "
        f"{list_available_files(file_context)}"
    )


async def load_scoped_file_ref(
    file_id: str | None,
    *,
    conversation_id: str | None,
    assignment_run_id: str | None,
):
    """The ``FileRef`` of a file THIS conversation or run holds, read from its row by id.

    The turn's ``FileContext`` is what the running loop knows it produced; the
    ``FileAttachment`` rows are what it produced. Every persistence path stamps
    the conversation or the run on the row at creation, so a file the context
    missed - persisted by a path that did not register it, or produced by an
    earlier execution of the same run (a re-run after a lost worker, a
    continuation) - is still this run's own. Only those two scopes are read,
    the ones the chat layer (``load_conversation_files``) and the resume
    (``load_run_generated_files``) load a context from, never a template
    asset, and only for a well-formed id: a filename is never looked up.
    ``None`` when nothing matches or the lookup fails (logged).
    """
    if not isinstance(file_id, str) or not file_id.strip():
        return None
    row_id = safe_uuid(_file_handle_key(file_id).removeprefix("file-"))
    conv_uuid = safe_uuid(conversation_id)
    run_uuid = safe_uuid(assignment_run_id)
    if row_id is None or (conv_uuid is None and run_uuid is None):
        return None

    from sqlalchemy import or_, select

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    scopes = []
    if conv_uuid is not None:
        scopes.append(FileAttachment.conversation_id == conv_uuid)
    if run_uuid is not None:
        scopes.append(FileAttachment.assignment_run_id == run_uuid)
    try:
        async with _own_session() as own_db:
            result = await own_db.execute(
                select(FileAttachment).where(
                    FileAttachment.id == row_id,
                    FileAttachment.template_container_id.is_(None),
                    or_(*scopes),
                )
            )
            row = result.scalars().first()
            return FileRef.from_attachment(row) if row is not None else None
    except Exception as exc:
        logger.warning(
            "Scoped file lookup failed for %s: %s", row_id, format_exception_message(exc)
        )
        return None
