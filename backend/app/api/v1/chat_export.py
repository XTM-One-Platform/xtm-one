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

"""A conversation exported as Markdown or PDF.

``GET /api/v1/chat/conversations/{id}/export?format=markdown|pdf``: readable by
whoever may read the conversation (``chat.resolve_conversation_for_read``: the
owner, a share, an org link), the unreadable answered exactly like the unknown
(404). The document is ``services/conversation_export.py``'s; a PDF render is
counted against the caller's export budget, like a deck's or a report's.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db, release_db_connection_before_loop
from app.core.utils import build_content_disposition
from app.models.user import User
from app.services.conversation_export import (
    conversation_markdown,
    conversation_pdf,
    export_filename,
    export_messages,
)
from app.services.file_responses import NO_STORE_CACHE_CONTROL

router = APIRouter()


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: str,
    format: str = Query("markdown", pattern="^(markdown|pdf)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from app.api.v1.chat import resolve_conversation_for_read

    conv, _access = await resolve_conversation_for_read(conversation_id, user, db)
    messages, truncated = await export_messages(db, conv.id)
    title = conv.title or "Conversation"
    markdown = conversation_markdown(title, messages, truncated=truncated)
    filename = export_filename(title, format)
    headers = {
        "Content-Disposition": build_content_disposition(filename),
        "Cache-Control": NO_STORE_CACHE_CONTROL,
    }
    if format == "markdown":
        return Response(content=markdown.encode("utf-8"), media_type="text/markdown; charset=utf-8", headers=headers)

    from app.services.presentation_export import enforce_export_rate_limit

    await enforce_export_rate_limit(user.id)
    await release_db_connection_before_loop(db)
    try:
        pdf = await conversation_pdf(title, markdown)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="The platform could not render the PDF. Try again later, or export as Markdown."
        ) from exc
    return Response(content=pdf, media_type="application/pdf", headers=headers)
