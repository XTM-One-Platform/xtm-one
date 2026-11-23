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

"""The AI Gateway's flagged embedding model, saved into an EMPTY platform embedding default (#1422).

Saved only where it re-embeds no stored vector: a re-embedding is hours of
heavy load. Once saved it is the platform's choice; a later flag never moves it.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from collections.abc import Iterable

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.llm.base import BaseLLMProvider
from app.llm.gateway_provider import FiligranGatewayProvider
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.embedding import Embedding
from app.models.llm_config import LLMProviderConfig

logger = logging.getLogger(__name__)

GATEWAY = FiligranGatewayProvider.provider_name

# A refused flag is not re-checked at every embedding lookup: the check reads
# the vector tables, and vectors rarely start matching. A failed size probe
# may be transient, so it is retried sooner.
_REFUSED_TTL_SECONDS = 300.0
_UNPROBED_TTL_SECONDS = 30.0
_refused_until: dict[tuple[uuid.UUID, str, int | None], float] = {}


def _dims(table) -> object:
    return func.coalesce(table.embedding_dimensions, func.vector_dims(table.embedding))


def no_embedding_choice(ps) -> bool:
    """No embedding default stored: an embedding-only provider (TEI) is a choice without a model."""
    return not (ps.embedding_model or ps.embedding_provider_id or ps.embedding_provider)


async def _native_size(provider: BaseLLMProvider, model: str) -> int | None:
    """The size *model* embeds at with no dimensions set, or ``None`` when the probe fails."""
    probe = copy.copy(provider)  # the shared instance keeps its dimensions
    probe.embedding_dimensions = None
    try:
        return len(await probe.embed("dimension probe", model=model)) or None
    except Exception as exc:
        logger.warning("AI Gateway embedding model %s not probed: %s", model, format_exception_message(exc))
        return None


async def _reembeds_nothing(
    db: AsyncSession, config: LLMProviderConfig, provider: BaseLLMProvider, model: str, dimensions: int | None
) -> bool | None:
    """Whether *model* from the gateway *config* re-embeds no stored vector; ``None`` when its size is unknown.

    True with no vector stored, or when every one is *config*'s *model* at one
    size: *dimensions* (the stored setting), else the model's native size.
    """
    chunk = (await db.execute(select(_dims(Embedding)).limit(1))).first()
    memory = (
        await db.execute(select(_dims(AgentMemoryEntry)).where(AgentMemoryEntry.embedding.is_not(None)).limit(1))
    ).first()
    if chunk is None and memory is None:
        return True
    size = (chunk or memory)[0]
    if dimensions is not None and dimensions != size:
        return False
    other_chunk = (
        select(Embedding.id)
        .where(
            or_(
                Embedding.embedding_provider.is_distinct_from(GATEWAY),
                # NULL once its config is deleted: the model and size still decide.
                and_(Embedding.embedding_provider_id.is_not(None), Embedding.embedding_provider_id != config.id),
                Embedding.embedding_model.is_distinct_from(model),
                _dims(Embedding).is_distinct_from(size),
            )
        )
        .exists()
    )
    # A memory entry records no provider: its model and size decide.
    other_memory = (
        select(AgentMemoryEntry.id)
        .where(
            AgentMemoryEntry.embedding.is_not(None),
            or_(
                AgentMemoryEntry.embedding_model.is_distinct_from(model),
                _dims(AgentMemoryEntry).is_distinct_from(size),
            ),
        )
        .exists()
    )
    if (await db.execute(select(or_(other_chunk, other_memory)))).scalar():
        return False
    if dimensions is not None:
        return True
    # Unset dimensions embed at the native size: stored vectors at any other would be re-embedded.
    # Probed last, once every stored vector is known to match.
    native = await _native_size(provider, model)
    return None if native is None else native == size


async def _flagged(
    configs: Iterable[LLMProviderConfig], dimensions: int | None
) -> list[tuple[LLMProviderConfig, BaseLLMProvider, str]]:
    """Each AI Gateway of *configs* with the embedding model it flags, those refused lately left out."""
    from app.llm.router import gateway_default

    out = []
    for config in configs:
        if config.provider != GATEWAY or not (listed := await gateway_default([config], "embedding")):
            continue
        if _refused_until.get((config.id, listed[2], dimensions), 0.0) <= time.monotonic():
            out.append(listed)
    return out


async def adoptable_gateway_default(
    db: AsyncSession, configs: Iterable[LLMProviderConfig], *, dimensions: int | None
) -> tuple[LLMProviderConfig, str] | None:
    """The first AI Gateway of *configs* flagging an embedding model that re-embeds nothing, and that model."""
    for config, provider, model in await _flagged(configs, dimensions):
        verdict = await _reembeds_nothing(db, config, provider, model, dimensions)
        if verdict:
            return config, model
        ttl = _UNPROBED_TTL_SECONDS if verdict is None else _REFUSED_TTL_SECONDS
        _refused_until[(config.id, model, dimensions)] = time.monotonic() + ttl
        if verdict is False:
            logger.info("AI Gateway default embedding model %s not used: stored vectors would be re-embedded", model)
    return None


async def fill_from_gateway(
    db: AsyncSession, ps, configs: Iterable[LLMProviderConfig]
) -> tuple[LLMProviderConfig, str] | None:
    """Save the gateway's flagged embedding model into *ps* when no embedding default is stored."""
    if not no_embedding_choice(ps):
        return None
    adopted = await adoptable_gateway_default(db, configs, dimensions=ps.embedding_dimensions)
    if adopted is not None:
        await _save(db, ps, *adopted)
        await db.flush()
    return adopted


async def _save(db: AsyncSession, ps, config: LLMProviderConfig, model: str) -> None:
    from app.models.knowledge_base import KnowledgeBase

    ps.embedding_provider = GATEWAY
    ps.embedding_provider_id = config.id
    ps.embedding_model = model
    # An empty knowledge base was labelled with a fallback model while none was set;
    # left so, the health check would ask to re-embed it.
    await db.execute(
        update(KnowledgeBase)
        .where(or_(KnowledgeBase.chunk_count == 0, KnowledgeBase.chunk_count.is_(None)))
        .where(KnowledgeBase.embedding_model.is_distinct_from(model))
        .values(embedding_model=model)
    )


async def byollm_keeps_it_empty(db: AsyncSession) -> bool:
    """Under ``BYOLLM_ACTIVATED``, an administrator-configured install's empty default is their choice."""
    from app.core.config import settings

    if not settings.byollm_activated:
        return False
    from app.main import _byollm_install_configured

    return await _byollm_install_configured(db)


async def fill_at_lookup(ps, configs: list[LLMProviderConfig]) -> tuple[LLMProviderConfig, str] | None:
    """:func:`fill_from_gateway` for an embedding lookup that found no model, committed in its own session.

    A choice saved meanwhile is copied onto *ps* instead, for the caller to
    resolve. While the caller's own transaction holds the row, the model serves
    this lookup unsaved, and a later lookup saves it.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.core import database
    from app.llm.router import select_provider_configs

    def _copy_to_caller(stored) -> None:
        for attr in ("embedding_provider", "embedding_provider_id", "embedding_model"):
            set_committed_value(ps, attr, getattr(stored, attr))

    # Flags are cached and refusals remembered: with no candidate, the lookup pays no query.
    if not await _flagged(configs, ps.embedding_dimensions):
        return None
    async with database.async_session() as db:
        stored = await peek_platform_settings(db)
        if stored is None:  # a fresh install's row, not committed yet: nothing configured
            return await adoptable_gateway_default(db, configs, dimensions=ps.embedding_dimensions)
        if not no_embedding_choice(stored):
            _copy_to_caller(stored)
            return None
        if await byollm_keeps_it_empty(db):
            return None
        configs = list((await db.execute(select_provider_configs())).scalars().all())
        adopted = await adoptable_gateway_default(db, configs, dimensions=stored.embedding_dimensions)
        if adopted is None:
            return None
        try:
            # Briefly: an admin's save commits in time, the caller's own transaction never does.
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            await db.refresh(stored, with_for_update=True)
        except DBAPIError:
            return adopted
        if not no_embedding_choice(stored):
            _copy_to_caller(stored)
            return None
        await _save(db, stored, *adopted)
        await db.commit()
    _copy_to_caller(stored)
    return adopted
