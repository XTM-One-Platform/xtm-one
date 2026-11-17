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

"""``read_conversation``: read a conversation the user referenced with ``@``.

Offered on a turn only when its conversation holds references the acting user
can open (`conversation_references.stored_references` filtered by
`readable_references`), the way the studio
tools ride on a bound conversation: it is not a suite an agent enables, and an
agent can never read a conversation the user did not point it at. Every call:

* reads only a conversation referenced in THIS conversation that the reader
  could open when the turn began (by id, or by the ``@key`` the message was
  sent with);
* re-checks that the acting user can open it (`chat.conversation_access`), so
  a share revoked after the reference was made is honoured at once;
* returns a window of its user and assistant messages, fenced as untrusted
  content, with the offset of the next window - never the whole transcript of
  a long conversation at once.

The description names those conversations by their stored key and id
(bounded) - never by a live title, which only a reader allowed to open the
conversation may learn. That is how a later turn of the conversation knows what
``@q3-report`` in an earlier message pointed at: nothing is added to the system
prompt (invariant 16) and the history keeps the messages as the user wrote them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.utils import safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.conversation_references import StoredReference

READ_CONVERSATION_TOOL = "read_conversation"

#: Messages one call returns at most, and by default.
MAX_READ_LIMIT = 30
DEFAULT_READ_LIMIT = 20
#: One message's text in a read, and a whole read's.
_MESSAGE_CHARS = 4_000
_READ_CHARS = 30_000


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()} [... {len(text) - limit} more characters]"


async def build_conversation_reader_tool(
    db: AsyncSession,
    user_id: str | uuid.UUID | None,
    conversation_id: str | uuid.UUID | None,
    *,
    referenced: list[StoredReference] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The reader tool for this turn, or ``None`` when nothing it may read is referenced.

    *referenced* is `stored_references` when the caller already read it (the
    turn did, to decide on the untrusted-content policy). Only the references
    the acting user can open NOW are named, and by the key the message was
    sent with - never by a live title: a conversation renamed after its share
    was revoked must tell the model nothing. Never raises: a
    lookup that fails costs the turn the tool, never the turn.
    """
    from app.services.conversation_references import (
        MAX_LISTED_REFERENCES,
        readable_references,
        stored_references,
    )

    acting_user_id = safe_uuid(str(user_id)) if user_id else None
    current_id = safe_uuid(str(conversation_id)) if conversation_id else None
    if acting_user_id is None or current_id is None:
        return None
    try:
        if referenced is None:
            referenced = await stored_references(db, current_id)
        readable = await readable_references(db, acting_user_id, referenced[:MAX_LISTED_REFERENCES])
    except Exception:  # the tool is optional, the turn is not
        from ._shared import logger

        logger.warning("read_conversation: listing the references of %s failed", current_id, exc_info=True)
        return None
    if not readable:
        return None
    listed = [f'@{ref.key} = "{ref.id}"' for ref in readable]

    def _resolve_target(target: str) -> uuid.UUID | None:
        """The listed reference *target* names: an id, or the ``@key`` it was sent with.

        Resolved against the references this turn lists only - what the reader
        could open when the turn began, by stored key - so asking for a key
        never says anything about a conversation the reader cannot open.
        """
        wanted = safe_uuid(target.strip())
        if wanted is not None:
            return wanted if any(ref.id == wanted for ref in readable) else None
        key = target.strip().lstrip("@").lower()
        matches = [ref.id for ref in readable if key and ref.key == key]
        return matches[0] if len(matches) == 1 else None

    async def _read_conversation(
        conversation_id: str,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> str:
        from app.core.database import async_session

        # Its own session: the calls of one batch run concurrently, and the
        # run's session is not the reader's to share.
        async with async_session() as session:
            return await _read(session, conversation_id, offset, limit)

    async def _read(session: AsyncSession, conversation_id: str, offset: int, limit: int) -> str:
        from app.api.v1.chat import conversation_access
        from app.models.agent import Agent
        from app.models.chat import Conversation, Message
        from app.models.user import User
        from app.services.content_safety import wrap_untrusted
        from app.services.conversation_references import (
            message_speaker,
            visible_message_filter,
        )

        target_id = _resolve_target(str(conversation_id or ""))
        if target_id is None:
            return (
                "Error: that is not a conversation the user referenced in this "
                "conversation. Only these can be read: " + "; ".join(listed) + ". "
                "The user references another one by tagging it with @ in a message."
            )
        user = await session.get(User, acting_user_id)
        row = (
            await session.execute(
                select(Conversation, Agent.name)
                .outerjoin(Agent, Agent.id == Conversation.agent_id)
                .where(Conversation.id == target_id)
            )
        ).first()
        if user is None or row is None or await conversation_access(session, user, row[0]) is None:
            # The same answer for a conversation deleted and one no longer
            # shared with the user: neither may be read, and which one it is
            # is not the agent's to learn.
            return (
                "Error: that conversation can no longer be opened by the user "
                "(it was deleted, or it is no longer shared with them)."
            )
        conv, agent_name = row
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        try:
            size = min(MAX_READ_LIMIT, max(1, int(limit)))
        except (TypeError, ValueError):
            size = DEFAULT_READ_LIMIT
        total = (
            await session.execute(
                select(func.count()).select_from(Message).where(visible_message_filter(conv.id))
            )
        ).scalar_one()
        if total == 0:
            return "That conversation holds no message yet."
        if start >= total:
            return (
                f"Offset {start} is past the end: the conversation holds {total} "
                f"messages (offsets 0 to {total - 1})."
            )
        rows = (
            await session.execute(
                select(Message.role, Message.content, Message.metadata_json, Message.created_at)
                .where(visible_message_filter(conv.id))
                .order_by(Message.sequence.asc())
                .offset(start)
                .limit(size)
            )
        ).all()
        lines: list[str] = []
        used = 0
        shown = 0
        for role, content, metadata_json, created_at in rows:
            when = f" {created_at:%Y-%m-%d %H:%M}" if created_at else ""
            entry = (
                f"#{start + shown + 1} [{message_speaker(role, metadata_json, agent_name)}{when}]\n"
                f"{_cut(content or '', _MESSAGE_CHARS)}"
            )
            if shown and used + len(entry) > _READ_CHARS:
                break
            lines.append(entry)
            used += len(entry)
            shown += 1
        end = start + shown
        header = f"Messages {start + 1} to {end} of {total} of the referenced conversation {conv.id}."
        footer = (
            f'More: read_conversation(conversation_id="{conv.id}", offset={end}).'
            if end < total
            else "That is the end of the conversation."
        )
        body = f"Title: {' '.join((conv.title or '').split())}\n\n" + "\n\n".join(lines)
        return "\n\n".join([header, wrap_untrusted(body, source="a referenced conversation"), footer])

    executor = CallableToolExecutor()
    executor.register(READ_CONVERSATION_TOOL, _read_conversation)
    definition = ToolDefinition(
        name=READ_CONVERSATION_TOOL,
        description=(
            "Read a conversation the user referenced in this conversation by tagging it "
            "with @. Returns its user and assistant messages in order, `limit` at a time "
            "from `offset` (0 = the first message), and the offset of the next window. "
            "Referenced here: " + "; ".join(listed) + "."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "The id of a referenced conversation (or the @key it was tagged with).",
                },
                "offset": {
                    "type": "integer",
                    "description": "The first message to return, 0-based (default 0).",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Messages to return, at most {MAX_READ_LIMIT} (default {DEFAULT_READ_LIMIT}).",
                    "default": DEFAULT_READ_LIMIT,
                },
            },
            "required": ["conversation_id"],
        },
        source="builtin",
    )
    return [definition], executor
