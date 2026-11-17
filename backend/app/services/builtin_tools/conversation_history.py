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

"""Read back one's own conversations, whatever agent held them.

A person's conversations are theirs, not their agents': when an agent is
deleted, or the person moves on to another one, the instructions they gave
stay in the conversation, and the agent they work with now must be able to
read them. Like filing one's own conversation (``workspace_filing``), that is
a person's right, not an agent capability someone must configure: this suite
is offered on every turn acting for an identified person, whatever the agent.
It holds two tools:

* ``list_conversations`` - the workspace tools' own (``builtin_tools.workspaces``),
  to find the conversation: by title, by the agent it was held with (a
  deleted one by its last name), by the video / presentation / report it is
  about. An agent with the agent-builder suite has it already, and then gets
  the reader alone;
* ``read_conversation_history`` - one conversation's stored turns, oldest
  first, a window at a time (``offset``, ``limit``, ``max_chars``), optionally
  only those holding a text (``search``): who wrote each (the user, or the
  agent by name, a deleted agent included), when, the tools a turn called and
  the tool results in one line each - never their payloads - and the files,
  decks, videos and reports a turn carried, as the markers and ids the other
  tools take. The current conversation is read when no id is named.

What may be read is the caller's own conversation and nothing else: the one
the turn runs in, or another of theirs that is one person's thread
(``workspace_knowledge.private_thread_clause``: never a group channel thread,
read by other people). Another person's conversation - shared with the caller
or not - answers like an unknown id. Reading another conversation than the
current one is audited once per window, as other cross-object reads
(``traces.audit_read_once``). Only what the conversation stores is read; the
LLM payloads of its turns are the trace tools' (``inspect_*_traces``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select

from app.core.utils import safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.chat import Conversation, Message

READ_TOOL = "read_conversation_history"
LIST_TOOL = "list_conversations"

#: Messages one call returns at most, and by default.
MAX_READ_LIMIT = 50
DEFAULT_READ_LIMIT = 20
#: Characters one call returns at most, and by default.
MAX_READ_CHARS = 40_000
DEFAULT_READ_CHARS = 20_000
_MIN_READ_CHARS = 1_000
#: Tool calls a turn's summary names before saying how many more there were.
_TOOLS_SHOWN = 20
_ANONYMOUS_REFUSAL = "Reading conversations is not available for anonymous users."
#: The widest offset accepted: past any conversation, so only ever "past the end".
_MAX_OFFSET = 10**9
_NOT_FOUND = (
    "Error: conversation `{id}` not found, or it is not one of the user's own conversations. "
    "Find the user's conversations with list_conversations."
)


def _int_arg(raw: object, default: int, low: int, high: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def listed_message_filter(conversation_id: uuid.UUID) -> Any:
    """The stored turns a reader of the conversation sees, tool turns included.

    Every user, assistant and tool message, but an assistant message with
    neither text nor tool calls, and the hidden wakes of background tasks
    (``parent_wake``), as ``conversation_references.visible_message_filter``
    leaves them out.
    """
    from app.models.chat import Message
    from app.services.parent_wake import PARENT_WAKE_SOURCE

    return and_(
        Message.conversation_id == conversation_id,
        Message.role.in_(("user", "assistant", "tool")),
        or_(Message.role != "assistant", Message.content != "", Message.tool_calls_json.is_not(None)),
        or_(Message.metadata_json.is_(None), ~Message.metadata_json.contains(PARENT_WAKE_SOURCE)),
    )


def _tool_summary(msg: Message, meta: dict[str, Any]) -> str | None:
    """The tools an assistant turn called, one line: name and outcome, never arguments."""
    calls: list[str] = []
    trace = meta.get("tool_call_trace")
    if isinstance(trace, list):
        for item in trace:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                calls.append(f"{item['name']} ({'ok' if item.get('success', True) else 'failed'})")
    if not calls and msg.tool_calls_json:
        try:
            raw = json.loads(msg.tool_calls_json)
        except (TypeError, ValueError):
            raw = []
        if isinstance(raw, list):
            calls = [str(c["name"]) for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str)]
    if not calls and isinstance(meta.get("tool_names"), list):
        calls = [str(n) for n in meta["tool_names"] if isinstance(n, str)]
    if not calls:
        return None
    shown = ", ".join(calls[:_TOOLS_SHOWN])
    more = len(calls) - _TOOLS_SHOWN
    return f"tools called: {shown}" + (f" (+{more} more)" if more > 0 else "")


def _files_line(meta: dict[str, Any]) -> str | None:
    """The files a turn carried, by name and the id the file tools take."""
    from app.services.content_safety import single_line

    attachments = meta.get("attachments")
    if not isinstance(attachments, list):
        return None
    files = [
        f"{single_line(str(a.get('filename') or 'file'))[:120]} (file_id {a['file_id']})"
        for a in attachments
        if isinstance(a, dict) and isinstance(a.get("file_id"), str)
    ]
    return f"files: {'; '.join(files)}" if files else None


def _speaker(
    msg: Message,
    meta: dict[str, Any],
    names: dict,
    *,
    legacy_author: tuple[bool, uuid.UUID | None],
) -> str:
    """Who wrote *msg*. *legacy_author* stands for a reply stored before
    authors were recorded: ``(True, the conversation's agent)`` when nothing
    else ever held the conversation, ``(False, None)`` when nobody can say."""
    from app.services.conversation_continuity import AGENT_SWITCH_KEY, label_name, message_author

    if msg.role == "user":
        return "user"
    if msg.role == "tool":
        return f"tool result: {msg.tool_name or 'tool'}"
    if AGENT_SWITCH_KEY in meta:
        return "note"
    known, author = message_author(meta)
    if not known:
        known, author = legacy_author
    label = label_name(author, names, known=known)
    deleted = known and author is not None and author in names and names[author].deleted
    return f"assistant: {label}" + (" (deleted agent)" if deleted else "")


def _cut(text: str, limit: int) -> tuple[str, int]:
    text = text.strip()
    if len(text) <= limit:
        return text, 0
    return text[:limit].rstrip(), len(text) - limit


def _render(
    msg: Message,
    position: int,
    *,
    meta: dict[str, Any],
    names: dict,
    legacy_author: tuple[bool, uuid.UUID | None],
    room: int,
) -> str:
    from app.channels.base import artifact_markers

    when = f" {msg.created_at:%Y-%m-%d %H:%M} UTC" if isinstance(msg.created_at, datetime) else ""
    header = f"#{position} [{_speaker(msg, meta, names, legacy_author=legacy_author)},{when}]"
    if msg.role == "tool":
        return f"{header} {len(msg.content or '')} characters of tool output (not shown)."
    body, hidden = _cut(msg.content or "", room)
    lines = [header]
    if body:
        lines.append(body)
    if hidden:
        lines.append(
            f"[... {hidden} more characters not shown: this message is longer than one "
            f"window ({MAX_READ_CHARS} characters at most)]"
        )
        kept = set(artifact_markers(body))
        cut_markers = [m for m in artifact_markers(msg.content or "") if m not in kept]
        if cut_markers:
            lines.append("markers further in this message: " + " ".join(cut_markers))
    for extra in (_files_line(meta), _tool_summary(msg, meta) if msg.role == "assistant" else None):
        if extra:
            lines.append(extra)
    return "\n".join(lines)


def build_conversation_history_tools(
    user_id: str,
    *,
    conversation_id: str | None,
    agent_id: str | None = None,
    with_listing: bool = True,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The suite; *with_listing* False when the agent-builder suite already
    offers ``list_conversations``. *conversation_id* is the turn's own
    conversation, read when no id is named; *agent_id* the agent reading,
    named in the audit."""
    from .workspaces import build_workspace_tools

    current = safe_uuid(conversation_id) if conversation_id else None
    caller = safe_uuid(user_id) if user_id and user_id != "anonymous" else None

    async def _read_conversation_history(
        conversation_id: str = "",
        offset: object = 0,
        limit: object = DEFAULT_READ_LIMIT,
        max_chars: object = DEFAULT_READ_CHARS,
        search: str = "",
    ) -> str:
        from app.core.database import async_session

        if caller is None:
            return _ANONYMOUS_REFUSAL
        # Its own session: the calls of one batch run concurrently.
        async with async_session() as session:
            return await _read(
                session,
                str(conversation_id or "").strip(),
                start=_int_arg(offset, 0, 0, _MAX_OFFSET),
                size=_int_arg(limit, DEFAULT_READ_LIMIT, 1, MAX_READ_LIMIT),
                budget=_int_arg(max_chars, DEFAULT_READ_CHARS, _MIN_READ_CHARS, MAX_READ_CHARS),
                search=str(search or "").strip(),
            )

    async def _target(session: AsyncSession, ref: str) -> Conversation | str:
        from app.models.chat import Conversation
        from app.services.workspace_knowledge import private_thread_clause

        if not ref:
            if current is None:
                return (
                    "Error: this call runs outside a conversation: name the conversation_id "
                    "(from list_conversations)."
                )
            wanted, own_thread = current, None
        else:
            wanted = safe_uuid(ref)
            if wanted is None:
                return f"Error: `{ref[:80]}` is not a valid conversation id."
            own_thread = None if wanted == current else private_thread_clause()
        query = select(Conversation).where(Conversation.id == wanted, Conversation.user_id == caller)
        if own_thread is not None:
            query = query.where(own_thread)
        conv = (await session.execute(query)).scalar_one_or_none()
        return conv if conv is not None else _NOT_FOUND.format(id=wanted)

    async def _read(session: AsyncSession, ref: str, *, start: int, size: int, budget: int, search: str) -> str:
        from app.api.v1.users import escape_like
        from app.models.chat import Message
        from app.models.user import User
        from app.services.chat_messages import message_order_asc, parse_message_metadata
        from app.services.content_safety import single_line, wrap_untrusted
        from app.services.conversation_continuity import (
            AUTHOR_AGENT_KEY,
            agent_labels,
            authors_from_traces,
            message_author,
            with_traced_names,
        )

        conv = await _target(session, ref)
        if isinstance(conv, str):
            return conv
        listed = listed_message_filter(conv.id)
        if search:
            listed = and_(listed, Message.content.ilike(f"%{escape_like(search)}%", escape="\\"))
        total = (await session.execute(select(func.count()).select_from(Message).where(listed))).scalar_one()
        about = f" matching '{single_line(search)[:80]}'" if search else ""
        if total == 0:
            return f"Conversation {conv.id} holds no message{about}."
        if start >= total:
            return f"Offset {start} is past the end: {total} message(s){about}, offsets 0 to {total - 1}."
        rows = list(
            (
                await session.execute(
                    select(Message).where(listed).order_by(*message_order_asc()).offset(start).limit(size)
                )
            )
            .scalars()
            .all()
        )
        metas = {m.id: parse_message_metadata(m.metadata_json) for m in rows}
        assistant = [m for m in rows if m.role == "assistant"]
        # A reply stored before authors were recorded: its run's trace says who
        # wrote it while it is kept, else its conversation's agent when nothing
        # else ever held the conversation.
        traced = await authors_from_traces(
            session, [m for m in assistant if not message_author(metas[m.id])[0]]
        )
        authors = {message_author(metas[m.id])[1] for m in assistant} | {t.agent_id for t in traced.values()}
        names = with_traced_names(
            await agent_labels(session, {*authors, conv.agent_id, conv.deleted_agent_id}), traced.values()
        )
        never_changed = conv.deleted_agent_id is None and (
            conv.original_agent_id is None or conv.original_agent_id == conv.agent_id
        )
        legacy_author = (True, conv.agent_id) if never_changed else (False, None)
        entries: list[str] = []
        used = 0
        for index, msg in enumerate(rows):
            if msg.id in traced:
                # Read as if stamped: the reader names the author the trace kept.
                agent = traced[msg.id].agent_id
                metas[msg.id] = {**metas[msg.id], AUTHOR_AGENT_KEY: str(agent) if agent else None}
            # A page holds whole messages: one that does not fit opens the
            # next page, where it has the whole window. Only a message longer
            # than a window is cut.
            entry = _render(
                msg, start + index, meta=metas[msg.id], names=names, legacy_author=legacy_author, room=budget
            )
            if entries and used + len(entry) > budget:
                break
            entries.append(entry)
            used += len(entry)
        end = start + len(entries)
        held = names.get(conv.agent_id) if conv.agent_id else None
        deleted = names.get(conv.deleted_agent_id) if conv.deleted_agent_id else None
        header = [
            f"Conversation {conv.id}: messages {start} to {end - 1} of {total}{about}, oldest first.",
            "Held with: "
            + (single_line(held.name)[:80] if held else "the General Assistant")
            + (
                f"; before that with {single_line(deleted.name)[:80]} "
                f"(agent since deleted, id `{conv.deleted_agent_id}`)"
                if deleted
                else ""
            )
            + ".",
        ]
        footer = (
            f'More: {READ_TOOL}(conversation_id="{conv.id}", offset={end}'
            + (f', search="{single_line(search)[:80]}"' if search else "")
            + ")."
            if end < total
            else "That is the end of the conversation" + (" for this search." if search else ".")
        )
        if conv.id != current:
            user = await session.get(User, caller)
            if user is not None:
                await _audit(session, user, conv)
        body = f"Title: {single_line(conv.title or '')}\n\n" + "\n\n".join(entries)
        return "\n".join(header) + "\n\n" + wrap_untrusted(body, source="a stored conversation") + "\n\n" + footer

    async def _audit(session: AsyncSession, user, conv: Conversation) -> None:
        from .traces import audit_read_once

        try:
            await audit_read_once(
                session,
                user,
                action="read_conversation",
                entity_type="conversation",
                entity_id=str(conv.id),
                summary=f"Read conversation '{(conv.title or '')[:120]}' from an agent",
                details={"calling_agent_id": agent_id, "via": "agent"},
            )
        except Exception:
            # The read is the user's own data: a failed audit write is logged,
            # never turned into a refusal to read it.
            logger.warning("read_conversation_history: auditing the read of %s failed", conv.id, exc_info=True)
            await session.rollback()

    definition = ToolDefinition(
        name=READ_TOOL,
        description=(
            "Read the stored messages of one of the user's own conversations - the current "
            "one when conversation_id is omitted - whatever agent held it, a deleted agent "
            "included. Use it to recover what the user asked in an earlier conversation or "
            "earlier in this one (instructions, decisions, the files, videos and decks it "
            "worked on). Returns messages oldest first, `limit` at a time from `offset` "
            "(0 = the first), each with its author (the user, or the agent by name), its "
            "date, the tools a turn called and the tool results in one line each (never "
            "their payloads), and the file / presentation / video / report markers and file "
            "ids the other tools accept; then the offset of the next window. `search` keeps "
            "only the messages containing a text. Find conversation ids with "
            "list_conversations. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "The conversation id (from list_conversations); omit it for this conversation.",
                    "default": "",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "The first message to return, 0-based; use the previous call's next offset.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LIMIT,
                    "default": DEFAULT_READ_LIMIT,
                    "description": f"Messages to return, at most {MAX_READ_LIMIT}.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": _MIN_READ_CHARS,
                    "maximum": MAX_READ_CHARS,
                    "default": DEFAULT_READ_CHARS,
                    "description": "Characters of message text this call returns at most.",
                },
                "search": {
                    "type": "string",
                    "default": "",
                    "description": "Only the messages containing this text (case-insensitive).",
                },
            },
            "required": [],
        },
        source="builtin",
    )
    executor = CallableToolExecutor()
    executor.register(READ_TOOL, _read_conversation_history)
    definitions = [definition]
    if with_listing:
        workspace_defs, handlers = build_workspace_tools(user_id, conversation_id=conversation_id)
        executor.register(LIST_TOOL, handlers[LIST_TOOL])
        definitions = [d for d in workspace_defs if d.name == LIST_TOOL] + definitions
    return definitions, executor
