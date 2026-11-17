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

"""Learning service – ingests data into knowledge bases for RAG."""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.llm.base import BaseLLMProvider
from app.llm.cut_answers import chat_retrying_a_cut, still_cut
from app.services.vector import store_embedding

logger = logging.getLogger(__name__)

# The providers' default ``max_tokens``, now explicit so an answer cut at it
# can be retried with twice the room.
_STYLE_GUIDE_MAX_TOKENS = 4096


async def _resolve_embedding_owner(
    db: AsyncSession,
    user_id: str,
    knowledge_base_id: str | None,
    agent_id: str | None,
) -> str | None:
    """Return the owner to stamp on embeddings, or ``None`` when shared.

    Company-managed KBs / agents store their embeddings shared (``user_id``
    NULL) so that hybrid search — which filters ``user_id == me OR IS NULL`` —
    keeps returning them for every user. The owner is tracked on the KB/agent
    row itself, never on each embedding. Resolved once per ingestion call so
    the per-chunk loop doesn't issue a query per chunk.
    """
    try:
        if knowledge_base_id:
            from app.models.knowledge_base import KnowledgeBase

            is_cm = await db.scalar(
                select(KnowledgeBase.is_company_managed).where(
                    KnowledgeBase.id == uuid.UUID(knowledge_base_id)
                )
            )
            if is_cm:
                return None
        elif agent_id:
            from app.models.agent import Agent

            is_cm = await db.scalar(
                select(Agent.is_company_managed).where(
                    Agent.id == uuid.UUID(agent_id)
                )
            )
            if is_cm:
                return None
    except Exception as exc:
        logger.warning("Failed to resolve embedding owner, defaulting to user: %s", format_exception_message(exc))
    return user_id


async def ingest_documents(
    db: AsyncSession,
    embedding_provider: BaseLLMProvider,
    user_id: str,
    knowledge_base_id: str | None = None,
    agent_id: str | None = None,
    documents: list[dict[str, Any]] | None = None,
    embedding_model: str | None = None,
    embedding_provider_id: str | None = None,
) -> int:
    """Ingest a batch of documents into the vector store.

    Each document should have:
        - content (str): The text to embed
        - source_type (str): e.g. "email", "document", "markdown"
        - source_id (str, optional): External identifier
    """
    if not documents:
        return 0

    from app.services.chunking import chunk_text as _chunk

    effective_user_id = await _resolve_embedding_owner(
        db, user_id, knowledge_base_id, agent_id
    )

    count = 0
    provider_name = getattr(embedding_provider, "provider_name", None)
    for doc in documents:
        content = doc.get("content", "")
        if not content or len(content.strip()) < 10:
            continue

        try:
            chunks = _chunk(content, chunk_size=500, chunk_overlap=100)
            for chunk in chunks:
                embedding = await embedding_provider.embed(chunk, model=embedding_model)
                await store_embedding(
                    db=db,
                    user_id=effective_user_id,
                    source_type=doc.get("source_type", "document"),
                    content=chunk,
                    embedding=embedding,
                    knowledge_base_id=knowledge_base_id,
                    agent_id=agent_id,
                    source_id=doc.get("source_id"),
                    embedding_provider=provider_name,
                    embedding_provider_id=embedding_provider_id,
                    embedding_model=embedding_model,
                    embedding_dimensions=len(embedding),
                )
                count += 1
        except Exception as e:
            logger.error("Failed to ingest document: %s", format_exception_message(e), exc_info=True)

    return count


async def learn_email_style(
    db: AsyncSession,
    llm: BaseLLMProvider,
    embedding_provider: BaseLLMProvider,
    user_id: str,
    agent_id: str | None = None,
    emails: list[dict[str, str]] | None = None,
    embedding_model: str | None = None,
    model: str | None = None,
) -> str:
    """Analyze email samples to extract writing style, then store as embeddings.

    *model* is the resolved chat model (agent default -> user preference ->
    platform default); callers must pass it so style analysis runs on the
    configured default chat model instead of the provider's built-in default.
    """
    if not emails:
        return "No emails provided."

    # Combine for analysis
    samples = "\n\n---\n\n".join(
        f"To: {e.get('to', 'N/A')}\nSubject: {e.get('subject', 'N/A')}\n\n{e.get('body', '')}"
        for e in emails[:10]
    )

    from app.llm.base import ChatMessage

    async def _analyze(max_tokens: int):
        return await llm.chat(
            messages=[
                ChatMessage(role="system", content="You are a writing style analyst."),
                ChatMessage(
                    role="user",
                    content=f"Analyze these email samples and extract a concise style guide:\n\n{samples}",
                ),
            ],
            model=model,
            max_tokens=max_tokens,
        )

    result, cut = await chat_retrying_a_cut(
        llm, model, _STYLE_GUIDE_MAX_TOKENS, _analyze, label="Style analysis"
    )
    if cut:
        # A guide cut short is not stored as the user's style: nothing is,
        # as when the analysis call fails.
        raise still_cut("Style analysis")
    style_guide = result.content or "Unable to analyze style."

    # Store the style guide as an embedding for future reference.
    #
    # Personal writing style is per-user even when the agent is
    # company-managed: it is learned from THIS user's sent emails. Stamp the
    # embedding with the requesting ``user_id`` (never the shared NULL owner)
    # so hybrid search — which matches ``user_id == me OR user_id IS NULL`` —
    # can't surface one user's learned style to other users of the same shared
    # agent. The shared-owner (``user_id`` NULL) path applies only to
    # intentionally shared KB / agent document ingestion in ``ingest_documents``.
    try:
        embedding = await embedding_provider.embed(style_guide, model=embedding_model)
        await store_embedding(
            db=db,
            user_id=user_id,
            source_type="style_guide",
            content=style_guide,
            embedding=embedding,
            agent_id=agent_id,
            embedding_provider=getattr(embedding_provider, "provider_name", None),
            embedding_model=embedding_model,
            embedding_dimensions=len(embedding),
        )
    except Exception as e:
        logger.warning("Failed to store style guide embedding: %s", format_exception_message(e))

    return style_guide
