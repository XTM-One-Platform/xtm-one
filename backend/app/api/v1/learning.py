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

"""Learning API endpoints – trigger data ingestion for knowledge bases."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_ai_enabled
from app.core.database import get_db
from app.llm.router import get_embedding_provider, get_llm_for_user
from app.models.user import User

router = APIRouter()


class IngestRequest(BaseModel):
    knowledge_base_id: str | None = None
    agent_id: str | None = None
    documents: list[dict]


class IngestResponse(BaseModel):
    ingested_count: int


class StyleLearnRequest(BaseModel):
    agent_id: str | None = None
    emails: list[dict]


class StyleLearnResponse(BaseModel):
    style_guide: str


@router.post("/ingest", response_model=IngestResponse)
async def ingest_documents(
    body: IngestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Ingest documents into a knowledge base or agent's vector store."""
    from app.services.learning import ingest_documents as _ingest

    try:
        embedding_provider, embedding_model, provider_config_id = await get_embedding_provider(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    count = await _ingest(
        db,
        embedding_provider,
        str(user.id),
        knowledge_base_id=body.knowledge_base_id,
        agent_id=body.agent_id,
        documents=body.documents,
        embedding_model=embedding_model,
        embedding_provider_id=str(provider_config_id) if provider_config_id else None,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return IngestResponse(ingested_count=count)


@router.post("/email-style", response_model=StyleLearnResponse)
async def learn_email_style(
    body: StyleLearnRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Analyze sent emails to learn the user's writing style."""
    from app.services.learning import learn_email_style as _learn

    try:
        # Resolve provider AND model via the standard chain (agent default ->
        # user preference -> platform default) so style learning runs on the
        # configured default chat model with failover, instead of the
        # first-configured provider's built-in default model.
        llm, style_model = await get_llm_for_user(db, str(user.id))
        embedding, embedding_model, _provider_id = await get_embedding_provider(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    style_guide = await _learn(
        db,
        llm,
        embedding,
        str(user.id),
        agent_id=body.agent_id,
        emails=body.emails,
        embedding_model=embedding_model,
        model=style_model,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return StyleLearnResponse(style_guide=style_guide)
