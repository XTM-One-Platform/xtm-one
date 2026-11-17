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

"""A conversation as a Markdown or PDF document.

The export the chat's conversation menu downloads: every user and assistant
message in order, under who wrote it and when. A chart block a reply carries
(``chat_charts``) is written as its table - a document has no chart viewer, and
the numbers stay readable. The PDF is that Markdown, rendered by the headless
Chromium every PDF of the platform goes through (``pdf_render``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Message
from app.services.chat_charts import charts_as_tables
from app.services.chat_messages import parse_message_metadata

#: The roles an export writes: what the chat shows as the conversation.
EXPORT_ROLES = ("user", "assistant")
#: The most messages one export carries (the newest ones past it are kept).
MAX_EXPORT_MESSAGES = 2000

EXPORT_FORMATS = ("markdown", "pdf")


async def export_messages(db: AsyncSession, conversation_id: uuid.UUID) -> tuple[list[Message], bool]:
    """The conversation's messages, chronological, and whether older ones were left out."""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role.in_(EXPORT_ROLES))
            .order_by(Message.sequence.desc(), Message.created_at.desc(), Message.id.desc())
            .limit(MAX_EXPORT_MESSAGES + 1)
        )
    ).scalars().all()
    truncated = len(rows) > MAX_EXPORT_MESSAGES
    kept = [row for row in rows[:MAX_EXPORT_MESSAGES] if not parse_message_metadata(row.metadata_json).get("hidden")]
    kept.reverse()
    return kept, truncated


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def conversation_markdown(
    title: str,
    messages: list[Message],
    *,
    truncated: bool = False,
    exported_at: datetime | None = None,
) -> str:
    """The Markdown document of *messages*, charts written as tables."""
    lines = [f"# {title or 'Conversation'}", "", f"_Exported {_when(exported_at or datetime.now(UTC))}_", ""]
    if truncated:
        lines += [f"_Only the {MAX_EXPORT_MESSAGES} most recent messages are included._", ""]
    for message in messages:
        speaker = "User" if message.role == "user" else "Assistant"
        lines += ["---", "", f"**{speaker}** - {_when(message.created_at)}", ""]
        lines += [charts_as_tables(message.content or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


async def conversation_pdf(title: str, markdown: str) -> bytes:
    from app.services.pdf_render import render_text_to_pdf

    return await render_text_to_pdf(markdown, title=title or "Conversation")


def export_filename(title: str, export_format: str) -> str:
    from app.core.utils import slugify

    stem = slugify(title or "") or "conversation"
    return f"{stem[:80]}.{'md' if export_format == 'markdown' else 'pdf'}"
