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

"""Search a workspace's conversations.

Offered on a turn whose workspace knowledge (``services.workspace_knowledge``)
holds a Conversations knowledge base. Every call reads which documents are no
longer searchable (``workspace_conversation_index.unsearchable_document_ids``):
a conversation unfiled, moved, archived or deleted, or whose owner lost the
workspace, is gone from the next search,
and the conversation the turn runs in is never returned to itself.

Results are other people's words: each passage is quarantined, and the
conversation it comes from is named by a link whose label - the title a
person typed - is escaped (``core.markdown_links``).
"""

from __future__ import annotations

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

TOOL_NAME = "search_workspace_conversations"
MAX_LIMIT = 20
_QUERY_MAX_CHARS = 1000


def build_workspace_conversation_search_tool(
    *,
    workspace_name: str,
    kb_id: str,
    current_conversation_id: str | None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The ``search_workspace_conversations`` tool for one workspace."""
    from app.core.utils import safe_uuid

    current = safe_uuid(current_conversation_id) if current_conversation_id else None

    async def _search(query: str = "", limit: int = 5) -> str:
        from app.core.database import async_session
        from app.core.markdown_links import markdown_link
        from app.llm.router import get_embedding_provider
        from app.services.channel_continuation import conversation_link
        from app.services.content_safety import wrap_untrusted
        from app.services.hybrid_search import hybrid_search_embeddings
        from app.services.workspace_conversation_index import (
            conversations_of,
            unsearchable_document_ids,
        )

        text = " ".join(str(query or "").split())[:_QUERY_MAX_CHARS]
        if not text:
            return "The `query` argument is required: say what to look for in the workspace's conversations."
        try:
            count = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            count = 5
        try:
            async with async_session() as db:
                excluded = await unsearchable_document_ids(db, kb_id, exclude_conversation_id=current)
                provider, model, _provider_id = await get_embedding_provider(db)
            embedding = await provider.embed(text, model=model)
            async with async_session() as db:
                hits = await hybrid_search_embeddings(
                    db,
                    query_text=text,
                    query_embedding=embedding,
                    knowledge_base_ids=[kb_id],
                    # Over-fetch by what will be dropped: the dropped set is the
                    # few documents the sweep has not reclaimed yet.
                    top_k=min(count + len(excluded), 100),
                    expected_embedding_dimensions=len(embedding),
                    expected_embedding_model=model,
                )
                hits = [hit for hit in hits if str(hit.source_id) not in excluded][:count]
                found = await conversations_of(db, [str(hit.source_id) for hit in hits])
        except Exception:
            logger.warning("Workspace conversation search failed", exc_info=True)
            return "The workspace's conversations could not be searched right now."
        blocks: list[str] = []
        for number, hit in enumerate(hits, start=1):
            if (match := found.get(str(hit.source_id))) is None:
                continue
            conversation_id, title = match
            link = markdown_link(title, conversation_link(conversation_id), fallback="Untitled conversation")
            blocks.append(
                f"[{number}] {link}\n"
                + wrap_untrusted(hit.content or "", f"workspace conversation {conversation_id}")
            )
        if not blocks:
            return "No conversation of this workspace matches."
        return (
            "Passages from this workspace's other conversations. Cite the conversation "
            "link you use.\n\n" + "\n\n".join(blocks)
        )

    executor = CallableToolExecutor()
    executor.register(TOOL_NAME, _search)
    definition = ToolDefinition(
        name=TOOL_NAME,
        description=(
            f"Search the earlier conversations of the workspace '{_one_line(workspace_name)}' "
            "- what its members asked, decided, found or produced - and get passages "
            "with a link to each conversation. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "limit": {
                    "type": "integer",
                    "description": f"Passages to return, 1 to {MAX_LIMIT} (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    )
    return [definition], executor


def _one_line(text: str) -> str:
    from app.services.content_safety import single_line

    return single_line(text or "")[:200]
